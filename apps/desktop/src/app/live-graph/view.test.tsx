import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import {
  type LiveGraphEdge,
  type LiveGraphNode,
  type LiveGraphPulse,
  type LiveGraphSnapshot,
  normalizeLiveGraphStatus
} from './model'
import {
  analyzeLiveGraphTopology,
  clientToLiveGraphPoint,
  DEFAULT_LIVE_GRAPH_VIEW_STATE,
  fitLiveGraphCamera,
  LIVE_GRAPH_KINDS,
  LIVE_GRAPH_MIN_SCALE,
  LiveGraphCanvas,
  liveGraphComponentCollisionShares,
  liveGraphComponentSettleTickLimit,
  liveGraphComponentShiftLimit,
  liveGraphComponentWorkTickLimit,
  liveGraphCoolingAlphaDecay,
  liveGraphCoolingTickBudget,
  liveGraphDenseLod,
  liveGraphDescendantHubIds,
  liveGraphEdgeAppearance,
  liveGraphEdgePath,
  liveGraphMotionSettled,
  liveGraphNodeRadii,
  liveGraphNodeRadius,
  LiveGraphPaneView,
  liveGraphReheatAlpha,
  liveGraphSemanticLinkStrength,
  liveGraphSemanticLinkSurfaceGap,
  liveGraphSemanticReachCounts,
  liveGraphSettleTickBudget,
  liveGraphSimulationHeat,
  liveGraphStartupHeat,
  liveGraphTextFadeOpacity,
  liveGraphTopologyKey,
  normalizeLiveGraphViewState,
  selectLiveGraphOverviewLabelIds,
  settledLiveGraphLayout,
  trimLiveGraphEdge,
  visibleLiveGraph
} from './view'

function graphNodePosition(node: Element): { x: number; y: number } {
  const match = node.getAttribute('transform')?.match(/translate\(([-\d.]+) ([-\d.]+)\)/)

  if (!match) {
    throw new Error('Graph node has no translate transform')
  }

  return { x: Number(match[1]), y: Number(match[2]) }
}

function graphCamera(world: Element): { scale: number; x: number; y: number } {
  const match = world.getAttribute('transform')?.match(/translate\(([-\d.]+) ([-\d.]+)\) scale\(([-\d.]+)\)/)

  if (!match) {
    throw new Error('Graph world has no camera transform')
  }

  return { scale: Number(match[3]), x: Number(match[1]), y: Number(match[2]) }
}

function nodeSurfaceGap(
  snapshot: LiveGraphSnapshot,
  layout: ReturnType<typeof settledLiveGraphLayout>,
  leftId: string,
  rightId: string
): number {
  const positionById = new Map(layout.map(node => [node.id, node]))

  const radiusById = liveGraphNodeRadii(
    snapshot.nodes,
    liveGraphSemanticReachCounts(snapshot.nodes, snapshot.edges),
    100,
    liveGraphDescendantHubIds(snapshot.nodes, snapshot.edges)
  )

  const left = positionById.get(leftId)!
  const right = positionById.get(rightId)!

  return Math.hypot(left.x - right.x, left.y - right.y) - (radiusById.get(leftId) ?? 0) - (radiusById.get(rightId) ?? 0)
}

function nodeCenterDistance(
  layout: ReturnType<typeof settledLiveGraphLayout>,
  leftId: string,
  rightId: string
): number {
  const positionById = new Map(layout.map(node => [node.id, node]))
  const left = positionById.get(leftId)!
  const right = positionById.get(rightId)!

  return Math.hypot(left.x - right.x, left.y - right.y)
}

function componentBounds(
  snapshot: LiveGraphSnapshot,
  layout: ReturnType<typeof settledLiveGraphLayout>,
  ids: readonly string[]
): { bottom: number; left: number; right: number; top: number } {
  const positions = new Map(layout.map(node => [node.id, node]))

  const radiusById = liveGraphNodeRadii(
    snapshot.nodes,
    liveGraphSemanticReachCounts(snapshot.nodes, snapshot.edges),
    100,
    liveGraphDescendantHubIds(snapshot.nodes, snapshot.edges)
  )

  return ids.reduce(
    (bounds, id) => {
      const position = positions.get(id)!
      const radius = radiusById.get(id) ?? 0

      return {
        bottom: Math.max(bounds.bottom, position.y + radius),
        left: Math.min(bounds.left, position.x - radius),
        right: Math.max(bounds.right, position.x + radius),
        top: Math.min(bounds.top, position.y - radius)
      }
    },
    { bottom: -Infinity, left: Infinity, right: -Infinity, top: Infinity }
  )
}

function renderedMinimumNodeClearance(container: HTMLElement, ids: readonly string[]): number {
  const members = ids.map(id => {
    const element = container.querySelector(`[data-live-graph-node-id="${id}"]`)

    if (!element) {
      throw new Error(`Graph node ${id} was not rendered`)
    }

    const position = graphNodePosition(element)
    const radius = Number(element.querySelector('[data-live-graph-node-body]')?.getAttribute('r')) + 4

    return { position, radius }
  })

  return Math.min(
    ...members.flatMap((left, leftIndex) =>
      members
        .slice(leftIndex + 1)
        .map(
          right =>
            Math.hypot(left.position.x - right.position.x, left.position.y - right.position.y) -
            left.radius -
            right.radius
        )
    )
  )
}

const graph: LiveGraphSnapshot = {
  edges: [
    {
      id: 'edge:session-workflow',
      kind: 'contains',
      sourceId: 'session:default:root',
      targetId: 'workflow:default:board:wf'
    },
    {
      id: 'edge:workflow-task',
      kind: 'contains',
      sourceId: 'workflow:default:board:wf',
      targetId: 'task:default:board:task'
    },
    {
      id: 'edge:task-agent',
      kind: 'delegated_to',
      sourceId: 'task:default:board:task',
      targetId: 'agent:default:board:task:worker'
    },
    {
      id: 'edge:task-artifact',
      kind: 'produced',
      sourceId: 'task:default:board:task',
      targetId: 'artifact:default:result'
    }
  ],
  nodes: [
    {
      entityId: 'worker',
      id: 'agent:default:board:task:worker',
      kind: 'agent',
      label: 'elephant',
      status: 'running'
    },
    {
      entityId: 'result',
      id: 'artifact:default:result',
      kind: 'artifact',
      label: 'Verification report'
    },
    {
      entityId: 'orphan',
      id: 'artifact:default:orphan',
      kind: 'artifact',
      label: 'Detached note'
    },
    {
      entityId: 'root',
      id: 'session:default:root',
      kind: 'session',
      label: 'Agent-ready codebase'
    },
    {
      assignee: 'reviewer-qa',
      board: 'board',
      detail: 'Exercise the installed app and capture the selected task state.',
      entityId: 'task',
      id: 'task:default:board:task',
      kind: 'task',
      label: 'Verify live app',
      priority: 2,
      result: 'The live verification passed.',
      status: 'running',
      summary: 'Desktop interaction is ready for review.',
      workflowId: 'wf'
    },
    {
      board: 'board',
      entityId: 'wf',
      id: 'workflow:default:board:wf',
      kind: 'workflow',
      label: 'Correct artifact',
      workflowId: 'wf'
    }
  ]
}

const sixKindGraph: LiveGraphSnapshot = {
  edges: [
    { id: 'edge:session-project', kind: 'contains', sourceId: 'session:types', targetId: 'project:types' },
    { id: 'edge:project-workflow', kind: 'contains', sourceId: 'project:types', targetId: 'workflow:types' },
    { id: 'edge:workflow-task', kind: 'contains', sourceId: 'workflow:types', targetId: 'task:running' },
    { id: 'edge:workflow-blocked', kind: 'contains', sourceId: 'workflow:types', targetId: 'task:blocked' },
    { id: 'edge:task-agent', kind: 'delegated_to', sourceId: 'task:running', targetId: 'agent:types' },
    { id: 'edge:task-artifact', kind: 'produced', sourceId: 'task:running', targetId: 'artifact:types' }
  ],
  nodes: [
    { entityId: 'types', id: 'session:types', kind: 'session', label: 'Session', status: 'queued' },
    { entityId: 'types', id: 'project:types', kind: 'project', label: 'Project', status: 'queued' },
    { entityId: 'types', id: 'workflow:types', kind: 'workflow', label: 'Workflow', status: 'running' },
    { entityId: 'running', id: 'task:running', kind: 'task', label: 'Running task', status: 'running' },
    { entityId: 'blocked', id: 'task:blocked', kind: 'task', label: 'Blocked task', status: 'blocked' },
    { entityId: 'types', id: 'agent:types', kind: 'agent', label: 'Agent', status: 'completed' },
    { entityId: 'types', id: 'artifact:types', kind: 'artifact', label: 'Artifact', status: 'failed' }
  ],
  rootId: 'session:types'
}

function twoSessionGraph(): LiveGraphSnapshot {
  return {
    edges: [
      {
        id: 'edge:session-a-workflow',
        kind: 'contains',
        sourceId: 'session:default:a',
        targetId: 'workflow:default:board:a'
      },
      {
        id: 'edge:workflow-a-task',
        kind: 'contains',
        sourceId: 'workflow:default:board:a',
        targetId: 'task:default:board:a'
      },
      {
        id: 'edge:session-b-workflow',
        kind: 'contains',
        sourceId: 'session:default:b',
        targetId: 'workflow:default:board:b'
      },
      {
        id: 'edge:workflow-b-task',
        kind: 'contains',
        sourceId: 'workflow:default:board:b',
        targetId: 'task:default:board:b'
      }
    ],
    nodes: [
      { entityId: 'a', id: 'session:default:a', kind: 'session', label: 'Session A' },
      { entityId: 'b', id: 'session:default:b', kind: 'session', label: 'Session B' },
      {
        board: 'board',
        entityId: 'a',
        id: 'workflow:default:board:a',
        kind: 'workflow',
        label: 'Workflow A'
      },
      {
        board: 'board',
        entityId: 'b',
        id: 'workflow:default:board:b',
        kind: 'workflow',
        label: 'Workflow B'
      },
      { board: 'board', entityId: 'a', id: 'task:default:board:a', kind: 'task', label: 'Task A' },
      { board: 'board', entityId: 'b', id: 'task:default:board:b', kind: 'task', label: 'Task B' }
    ],
    rootId: 'session:default:a'
  }
}

function denseReadabilityGraph(): LiveGraphSnapshot {
  const nodes: LiveGraphSnapshot['nodes'] = [
    { entityId: 'session', id: 'session:root', kind: 'session', label: 'Session' },
    { entityId: 'project', id: 'project:root', kind: 'project', label: 'Project' },
    { entityId: 'workflow-a', id: 'workflow:a', kind: 'workflow', label: 'Workflow A' },
    { entityId: 'workflow-b', id: 'workflow:b', kind: 'workflow', label: 'Workflow B' },
    { entityId: 'selected', id: 'task:selected', kind: 'task', label: 'Selected task' },
    { entityId: 'neighbor', id: 'agent:neighbor', kind: 'agent', label: 'Neighbor agent' },
    { entityId: 'unrelated', id: 'task:unrelated', kind: 'task', label: 'Unrelated task' }
  ]

  for (let index = 0; index < 793; index += 1) {
    nodes.push({
      entityId: String(index),
      id: `artifact:orphan:${index}`,
      kind: 'artifact',
      label: `Orphan ${index}`
    })
  }

  return {
    edges: [
      { id: 'edge:session-project', kind: 'contains', sourceId: 'session:root', targetId: 'project:root' },
      { id: 'edge:project-workflow-a', kind: 'contains', sourceId: 'project:root', targetId: 'workflow:a' },
      { id: 'edge:project-workflow-b', kind: 'contains', sourceId: 'project:root', targetId: 'workflow:b' },
      { id: 'edge:workflow-task', kind: 'contains', sourceId: 'workflow:a', targetId: 'task:selected' },
      { id: 'edge:task-agent', kind: 'delegated_to', sourceId: 'task:selected', targetId: 'agent:neighbor' },
      { id: 'edge:workflow-unrelated', kind: 'contains', sourceId: 'workflow:b', targetId: 'task:unrelated' }
    ],
    nodes,
    rootId: 'session:root'
  }
}

function denseManyEdgeGraph(): LiveGraphSnapshot {
  const snapshot = denseReadabilityGraph()
  const artifactCount = 793
  const edges = [...snapshot.edges]

  for (let step = 1; step <= 5; step += 1) {
    for (let index = 0; index < artifactCount; index += 1) {
      edges.push({
        id: `edge:artifact:${step}:${index}`,
        kind: 'depends_on',
        sourceId: `artifact:orphan:${index}`,
        targetId: `artifact:orphan:${(index + step) % artifactCount}`
      })
    }
  }

  return { ...snapshot, edges }
}

function forceStarGraph(): LiveGraphSnapshot {
  const hubId = 'workflow:force:hub'

  const nodes: LiveGraphSnapshot['nodes'] = [{ entityId: 'hub', id: hubId, kind: 'workflow', label: 'Force hub' }]

  const edges: LiveGraphSnapshot['edges'] = []

  for (let index = 0; index < 8; index += 1) {
    const id = `task:force:${index}`

    nodes.push({ entityId: String(index), id, kind: 'task', label: `Force task ${index}` })
    edges.push({ id: `edge:force:${index}`, kind: 'contains', sourceId: hubId, targetId: id })
  }

  return { edges, nodes }
}

function oneHopLinkGraph(kind: LiveGraphSnapshot['edges'][number]['kind']): LiveGraphSnapshot {
  const parentId = `task:one-hop:${kind}:parent`
  const childId = `task:one-hop:${kind}:child`

  return {
    edges: [
      {
        id: `edge:one-hop:${kind}`,
        kind,
        sourceId: kind === 'depends_on' ? childId : parentId,
        targetId: kind === 'depends_on' ? parentId : childId
      }
    ],
    nodes: [
      { entityId: 'parent', id: parentId, kind: 'task', label: 'Parent', workflowId: 'one-hop' },
      { entityId: 'child', id: childId, kind: 'task', label: 'Child', workflowId: 'one-hop' }
    ]
  }
}

function hierarchicalTaskGraph(): {
  graph: LiveGraphSnapshot
  ids: {
    agent: string
    artifact: string
    childTask: string
    rootTask: string
    session: string
    workflow: string
  }
} {
  const ids = {
    agent: 'agent:default:board:tree-child',
    artifact: 'artifact:default:board:tree-result',
    childTask: 'task:default:board:tree-child',
    rootTask: 'task:default:board:tree-root',
    session: 'session:default:tree',
    workflow: 'workflow:default:board:tree'
  }

  return {
    graph: {
      edges: [
        { id: 'edge:tree-session-workflow', kind: 'contains', sourceId: ids.session, targetId: ids.workflow },
        { id: 'edge:tree-workflow-root', kind: 'contains', sourceId: ids.workflow, targetId: ids.rootTask },
        { id: 'edge:tree-child-depends-root', kind: 'depends_on', sourceId: ids.childTask, targetId: ids.rootTask },
        { id: 'edge:tree-child-agent', kind: 'delegated_to', sourceId: ids.childTask, targetId: ids.agent },
        { id: 'edge:tree-agent-artifact', kind: 'produced', sourceId: ids.agent, targetId: ids.artifact }
      ],
      nodes: [
        { entityId: 'tree', id: ids.session, kind: 'session', label: 'Tree session' },
        {
          board: 'board',
          entityId: 'tree',
          id: ids.workflow,
          kind: 'workflow',
          label: 'Tree workflow',
          workflowId: 'tree'
        },
        {
          board: 'board',
          entityId: 'tree-root',
          id: ids.rootTask,
          kind: 'task',
          label: 'Tree root',
          workflowId: 'tree'
        },
        {
          board: 'board',
          entityId: 'tree-child',
          id: ids.childTask,
          kind: 'task',
          label: 'Tree child',
          workflowId: 'tree'
        },
        { board: 'board', entityId: 'tree-agent', id: ids.agent, kind: 'agent', label: 'Tree agent' },
        { entityId: 'tree-result', id: ids.artifact, kind: 'artifact', label: 'Tree result' }
      ],
      rootId: ids.session
    },
    ids
  }
}

function denseCollisionGraph(nodeCount = 800): LiveGraphSnapshot {
  const hubId = 'workflow:collision:hub'
  const nodes: LiveGraphSnapshot['nodes'] = [{ entityId: 'hub', id: hubId, kind: 'workflow', label: 'Collision hub' }]
  const edges: LiveGraphSnapshot['edges'] = []

  for (let index = 1; index < nodeCount; index += 1) {
    const id = `task:collision:${index}`

    nodes.push({ entityId: String(index), id, kind: 'task', label: `Collision task ${index}` })
    edges.push({ id: `edge:collision:${index}`, kind: 'contains', sourceId: hubId, targetId: id })
  }

  return { edges, nodes, rootId: hubId }
}

function disconnectedGraph(nodeCount = 37): LiveGraphSnapshot {
  const kinds = ['session', 'project', 'workflow', 'task', 'agent', 'artifact'] as const

  return {
    edges: [],
    nodes: Array.from({ length: nodeCount }, (_, index) => ({
      entityId: String(index),
      id: `orphan:${String(index).padStart(2, '0')}`,
      kind: kinds[index % kinds.length]!,
      label: `Orphan ${index}`
    }))
  }
}

function minimumVisibleNodeClearance(
  snapshot: LiveGraphSnapshot,
  layout: ReturnType<typeof settledLiveGraphLayout>
): { clearance: number; left: string; right: string } {
  const analysis = analyzeLiveGraphTopology(snapshot.nodes, snapshot.edges)
  const nodeById = new Map(snapshot.nodes.map(node => [node.id, node]))

  const radiusById = liveGraphNodeRadii(
    snapshot.nodes,
    liveGraphSemanticReachCounts(snapshot.nodes, snapshot.edges),
    100,
    liveGraphDescendantHubIds(snapshot.nodes, snapshot.edges)
  )

  let clearance = Infinity
  let closest: [string, string] = ['', '']

  for (let leftIndex = 0; leftIndex < layout.length; leftIndex += 1) {
    const left = layout[leftIndex]!
    const leftNode = nodeById.get(left.id)!
    const leftRadius = (radiusById.get(left.id) ?? liveGraphNodeRadius(leftNode, 0)) + 3.5

    for (let rightIndex = leftIndex + 1; rightIndex < layout.length; rightIndex += 1) {
      const right = layout[rightIndex]!
      const rightNode = nodeById.get(right.id)!
      const rightRadius = (radiusById.get(right.id) ?? liveGraphNodeRadius(rightNode, 0)) + 3.5

      const nextClearance = Math.hypot(left.x - right.x, left.y - right.y) - leftRadius - rightRadius

      if (nextClearance < clearance) {
        clearance = nextClearance
        closest = [left.id, right.id]
      }
    }
  }

  return { clearance, left: closest[0], right: closest[1] }
}

class TestResizeObserver {
  constructor(private callback: ResizeObserverCallback) {}

  disconnect() {}

  observe(target: Element) {
    this.callback(
      [
        {
          contentRect: {
            bottom: 600,
            height: 600,
            left: 0,
            right: 900,
            top: 0,
            width: 900,
            x: 0,
            y: 0,
            toJSON: () => ({})
          },
          target
        } as ResizeObserverEntry
      ],
      this as unknown as ResizeObserver
    )
  }

  unobserve() {}
}

beforeAll(() => {
  vi.stubGlobal('ResizeObserver', TestResizeObserver)
  Object.defineProperty(globalThis.document, 'hidden', { configurable: true, value: false })
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) =>
    globalThis.window.setTimeout(() => callback(performance.now()), 16)
  )
  vi.stubGlobal('cancelAnimationFrame', (id: number) => globalThis.window.clearTimeout(id))
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({
      addEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
      matches: false,
      media: '(prefers-reduced-motion: reduce)',
      onchange: null,
      removeEventListener: vi.fn()
    }))
  )
  Element.prototype.hasPointerCapture ??= () => false
  Element.prototype.setPointerCapture ??= () => undefined
  Element.prototype.releasePointerCapture ??= () => undefined
})

afterEach(() => {
  cleanup()
  window.localStorage.clear()
})

describe('Graph View model', () => {
  it('uses the full pane without a title or node-count header strip', async () => {
    const { container } = render(
      <LiveGraphPaneView descriptor={{ key: 'clean-pane', title: 'Session graph' }} graph={sixKindGraph} />
    )

    expect(await screen.findByRole('application', { name: 'Graph View' })).toBeTruthy()
    expect(container.querySelector('header')).toBeNull()
    expect(screen.queryByText('Session graph')).toBeNull()
    expect(screen.queryByText(/nodes · .* connections/)).toBeNull()
  })

  it('bounds synchronous settling for dense global graphs', () => {
    expect(liveGraphSettleTickBudget(100)).toBe(260)
    expect(liveGraphSettleTickBudget(500)).toBe(12)
    expect(liveGraphSettleTickBudget(1_000)).toBe(2)
    expect(liveGraphSettleTickBudget(2_500)).toBe(1)
  })

  it('bounds component packing and prevents tiny graphs from absorbing the full collision', () => {
    expect(liveGraphComponentSettleTickLimit(100)).toBe(120)
    expect(liveGraphComponentSettleTickLimit(2_500)).toBe(48)
    expect(liveGraphComponentWorkTickLimit(7, 6)).toBe(120)
    expect(liveGraphComponentWorkTickLimit(793, 3_965)).toBe(5)
    expect(liveGraphComponentShiftLimit(0)).toBe(3)
    expect(liveGraphComponentShiftLimit(0.25)).toBeCloseTo(8.25)
    expect(liveGraphComponentShiftLimit(1)).toBe(24)
    expect(liveGraphComponentCollisionShares(1_000, 1)).toEqual({ left: 0.25, right: 0.75 })
    expect(liveGraphComponentCollisionShares(1, 1_000)).toEqual({ left: 0.75, right: 0.25 })
  })

  it('counts unique visible neighbors and finds disconnected components deterministically', () => {
    const topology: LiveGraphSnapshot = {
      edges: [
        { id: 'edge:a-b', kind: 'contains', sourceId: 'task:a', targetId: 'task:b' },
        { id: 'edge:b-a', kind: 'depends_on', sourceId: 'task:b', targetId: 'task:a' },
        { id: 'edge:self', kind: 'produced', sourceId: 'task:a', targetId: 'task:a' },
        { id: 'edge:missing', kind: 'delegated_to', sourceId: 'task:a', targetId: 'agent:missing' }
      ],
      nodes: [
        { entityId: 'c', id: 'task:c', kind: 'task', label: 'C' },
        { entityId: 'a', id: 'task:a', kind: 'task', label: 'A' },
        { entityId: 'b', id: 'task:b', kind: 'task', label: 'B' }
      ]
    }

    const analysis = analyzeLiveGraphTopology(topology.nodes, topology.edges)
    const reversed = analyzeLiveGraphTopology([...topology.nodes].reverse(), [...topology.edges].reverse())

    expect([...analysis.degreeById]).toEqual([
      ['task:c', 0],
      ['task:a', 1],
      ['task:b', 1]
    ])
    expect(analysis.components).toEqual([['task:a', 'task:b'], ['task:c']])
    expect(reversed.components).toEqual(analysis.components)
    expect([...reversed.degreeById].sort()).toEqual([...analysis.degreeById].sort())
  })

  it('counts only unique owned descendants and does not double-count shared descendants', () => {
    const topology: LiveGraphSnapshot = {
      edges: [
        { id: 'edge:session-workflow', kind: 'contains', sourceId: 'session:root', targetId: 'workflow:root' },
        { id: 'edge:workflow-a', kind: 'contains', sourceId: 'workflow:root', targetId: 'task:a' },
        { id: 'edge:a-agent', kind: 'delegated_to', sourceId: 'task:a', targetId: 'agent:shared' },
        { id: 'edge:b-agent', kind: 'delegated_to', sourceId: 'task:b', targetId: 'agent:shared' },
        { id: 'edge:agent-artifact', kind: 'produced', sourceId: 'agent:shared', targetId: 'artifact:result' },
        { id: 'edge:b-depends-a', kind: 'depends_on', sourceId: 'task:b', targetId: 'task:a' }
      ],
      nodes: [
        { entityId: 'root', id: 'session:root', kind: 'session', label: 'Session' },
        { entityId: 'root', id: 'workflow:root', kind: 'workflow', label: 'Workflow', workflowId: 'root' },
        { entityId: 'a', id: 'task:a', kind: 'task', label: 'Task A', workflowId: 'root' },
        { entityId: 'b', id: 'task:b', kind: 'task', label: 'Task B', workflowId: 'root' },
        { entityId: 'shared', id: 'agent:shared', kind: 'agent', label: 'Shared agent' },
        { entityId: 'result', id: 'artifact:result', kind: 'artifact', label: 'Result' }
      ]
    }

    const counts = liveGraphSemanticReachCounts(topology.nodes, topology.edges)
    const reversed = liveGraphSemanticReachCounts([...topology.nodes].reverse(), [...topology.edges].reverse())

    expect(Object.fromEntries(counts)).toEqual({
      'agent:shared': 1,
      'artifact:result': 0,
      'session:root': 5,
      'task:a': 3,
      'task:b': 2,
      'workflow:root': 4
    })
    expect(reversed).toEqual(counts)
  })

  it('keeps cross-workflow task dependencies contextual instead of inflating hub size', () => {
    const nodes: LiveGraphNode[] = [
      {
        entityId: 'a',
        id: 'task:board:workflow-a:a',
        kind: 'task',
        label: 'Workflow A task',
        workflowId: 'workflow-a'
      },
      {
        entityId: 'b',
        id: 'task:board:workflow-b:b',
        kind: 'task',
        label: 'Workflow B task',
        workflowId: 'workflow-b'
      }
    ]

    const edges: LiveGraphEdge[] = [
      {
        id: 'edge:cross-workflow-context',
        kind: 'depends_on',
        sourceId: nodes[1]!.id,
        targetId: nodes[0]!.id
      }
    ]

    const counts = liveGraphSemanticReachCounts(nodes, edges)

    expect(Object.fromEntries(counts)).toEqual({
      [nodes[0]!.id]: 0,
      [nodes[1]!.id]: 0
    })
    expect(analyzeLiveGraphTopology(nodes, edges).degreeById.get(nodes[0]!.id)).toBe(1)
  })

  it('treats legacy task cycles as peers while preserving their shared downstream reach', () => {
    const nodes: LiveGraphNode[] = [
      {
        entityId: 'workflow',
        id: 'workflow:board:workflow',
        kind: 'workflow',
        label: 'Workflow',
        workflowId: 'workflow'
      },
      ...['a', 'b', 'c'].map(id => ({
        entityId: id,
        id: `task:board:workflow:${id}`,
        kind: 'task' as const,
        label: `Task ${id.toUpperCase()}`,
        workflowId: 'workflow'
      }))
    ]

    const edges: LiveGraphEdge[] = [
      {
        id: 'edge:workflow-a',
        kind: 'contains',
        sourceId: nodes[0]!.id,
        targetId: nodes[1]!.id
      },
      { id: 'edge:a-b', kind: 'depends_on', sourceId: nodes[2]!.id, targetId: nodes[1]!.id },
      { id: 'edge:b-a', kind: 'depends_on', sourceId: nodes[1]!.id, targetId: nodes[2]!.id },
      { id: 'edge:b-c', kind: 'depends_on', sourceId: nodes[3]!.id, targetId: nodes[2]!.id }
    ]

    expect(Object.fromEntries(liveGraphSemanticReachCounts(nodes, edges))).toEqual({
      [nodes[0]!.id]: 3,
      [nodes[1]!.id]: 1,
      [nodes[2]!.id]: 1,
      [nodes[3]!.id]: 0
    })
  })

  it('keeps a high-degree leaf at the shared base size', () => {
    const leaf = { entityId: 'shared', id: 'task:shared-leaf', kind: 'task' as const, label: 'Shared leaf' }

    const parents = Array.from({ length: 12 }, (_, index) => ({
      entityId: String(index),
      id: `workflow:parent:${index}`,
      kind: 'workflow' as const,
      label: `Workflow ${index}`
    }))

    const topology: LiveGraphSnapshot = {
      edges: parents.map((parent, index) => ({
        id: `edge:parent-leaf:${index}`,
        kind: 'contains' as const,
        sourceId: parent.id,
        targetId: leaf.id
      })),
      nodes: [...parents, leaf]
    }

    const counts = liveGraphSemanticReachCounts(topology.nodes, topology.edges)
    const radii = liveGraphNodeRadii(topology.nodes, counts)

    expect(analyzeLiveGraphTopology(topology.nodes, topology.edges).degreeById.get(leaf.id)).toBe(12)
    expect(counts.get(leaf.id)).toBe(0)
    expect(radii.get(leaf.id)).toBe(liveGraphNodeRadius(leaf, 0))
    expect(radii.get(parents[0]!.id)).toBeGreaterThan(radii.get(leaf.id)!)
  })

  it('keeps hierarchy and attachment links short and strong around their hubs', () => {
    const taskLinkGap = liveGraphSemanticLinkSurfaceGap('depends_on', 112)
    const hierarchyGap = liveGraphSemanticLinkSurfaceGap('contains', 112)
    const attachmentGap = liveGraphSemanticLinkSurfaceGap('delegated_to', 112)
    const weakHubDegreeStrength = 1 / 13

    expect(hierarchyGap).toBeLessThan(taskLinkGap / 2)
    expect(attachmentGap).toBeLessThan(hierarchyGap)
    expect(liveGraphSemanticLinkStrength('contains', weakHubDegreeStrength, 42)).toBeCloseTo(0.72)
    expect(liveGraphSemanticLinkStrength('delegated_to', weakHubDegreeStrength, 42)).toBeCloseTo(0.82)
    expect(liveGraphSemanticLinkStrength('depends_on', weakHubDegreeStrength, 42)).toBeCloseTo(weakHubDegreeStrength)
    expect(liveGraphSemanticLinkStrength('contains', weakHubDegreeStrength, 0)).toBe(0)
  })

  it('maps descendant counts to monotonic radii capped at the hub maximum', () => {
    const node = graph.nodes.find(candidate => candidate.kind === 'session')!
    const referenceCount = 600

    const radii = [0, 1, 4, 16, 100, referenceCount].map(count => liveGraphNodeRadius(node, count, referenceCount))

    for (let index = 1; index < radii.length; index += 1) {
      expect(radii[index]).toBeGreaterThan(radii[index - 1]!)
    }

    expect(radii.at(-1)).toBeCloseTo(40)
    expect(liveGraphNodeRadius(node, 1_000_000, referenceCount)).toBeCloseTo(40)
    expect(liveGraphNodeRadius(node, -1)).toBe(radii[0])
    expect(Number.isFinite(liveGraphNodeRadius(node, Number.NaN, Number.NaN))).toBe(true)
  })

  it('keeps relative hub sizing legible in a fitted dense overview', () => {
    const leaf = graph.nodes.find(candidate => candidate.kind === 'task')!
    const hub = graph.nodes.find(candidate => candidate.kind === 'session')!
    const fittedScale = 0.041_738_8
    const leafPixels = liveGraphNodeRadius(leaf, 0, 637) * fittedScale
    const hubPixels = liveGraphNodeRadius(hub, 637, 637) * fittedScale

    expect(leafPixels).toBeLessThanOrEqual(2)
    expect(hubPixels).toBeGreaterThanOrEqual(1.5)
    expect(hubPixels / leafPixels).toBeGreaterThan(3)
  })

  it('keeps dense overview labels deterministic, bounded, and workflow-led', () => {
    const denseGraph = denseReadabilityGraph()
    const analysis = analyzeLiveGraphTopology(denseGraph.nodes, denseGraph.edges)
    const labels = selectLiveGraphOverviewLabelIds(denseGraph.nodes, analysis, 3)
    const reversed = analyzeLiveGraphTopology([...denseGraph.nodes].reverse(), [...denseGraph.edges].reverse())

    expect([...labels].sort()).toEqual(['project:root', 'workflow:a', 'workflow:b'])
    expect([...selectLiveGraphOverviewLabelIds([...denseGraph.nodes].reverse(), reversed, 3)].sort()).toEqual(
      [...labels].sort()
    )
    expect([...labels].some(id => id.startsWith('artifact:orphan:'))).toBe(false)
  })

  it('fades labels smoothly across the configured zoom threshold', () => {
    expect(liveGraphDenseLod(0.2)).toBe('overview')
    expect(liveGraphDenseLod(0.5)).toBe('structure')
    expect(liveGraphDenseLod(1)).toBe('detail')

    const faded = liveGraphTextFadeOpacity(0.5, 55)

    expect(faded).toBeGreaterThan(0)
    expect(faded).toBeLessThan(1)
    expect(liveGraphTextFadeOpacity(0.5, 0)).toBeGreaterThan(faded)
    expect(liveGraphTextFadeOpacity(0.5, 100)).toBeLessThan(faded)
    expect(liveGraphTextFadeOpacity(1, 200)).toBe(0)
  })

  it('keeps dense resting edges faded and fully reveals an emphasized neighborhood', () => {
    expect(liveGraphEdgeAppearance(true, 'overview', null, 'a', 'b')).toEqual({
      emphasized: false,
      opacity: 0.14,
      strokeWidth: 0.65
    })
    expect(liveGraphEdgeAppearance(true, 'overview', 'a', 'a', 'b')).toEqual({
      emphasized: true,
      opacity: 1,
      strokeWidth: 1.8
    })
    expect(liveGraphEdgeAppearance(true, 'overview', 'x', 'a', 'b')).toEqual({
      emphasized: false,
      opacity: 0.025,
      strokeWidth: 0.5
    })
  })

  it('keeps every connected node eligible when no node is selected', () => {
    const depthOne = visibleLiveGraph(graph, {
      enabledKinds: new Set(DEFAULT_LIVE_GRAPH_VIEW_STATE.enabledKinds),
      focusDepth: 1,
      orphans: false,
      search: ''
    })

    const depthTwo = visibleLiveGraph(graph, {
      enabledKinds: new Set(DEFAULT_LIVE_GRAPH_VIEW_STATE.enabledKinds),
      focusDepth: 2,
      orphans: false,
      search: ''
    })

    const connectedKinds = ['agent', 'artifact', 'session', 'task', 'workflow']

    expect(depthOne.nodes.map(node => node.kind).sort()).toEqual(connectedKinds)
    expect(depthTwo.nodes.map(node => node.kind).sort()).toEqual(connectedKinds)
  })

  it('shows every connected node from every session cluster at one hop', () => {
    const globalGraph = twoSessionGraph()

    const depthOne = visibleLiveGraph(globalGraph, {
      enabledKinds: new Set(DEFAULT_LIVE_GRAPH_VIEW_STATE.enabledKinds),
      focusDepth: 1,
      orphans: false,
      search: ''
    })

    expect(depthOne.nodes.map(node => node.label).sort()).toEqual([
      'Session A',
      'Session B',
      'Task A',
      'Task B',
      'Workflow A',
      'Workflow B'
    ])
  })

  it('ignores open workflow containers while preserving the full context of active tasks', () => {
    const activeAndSettled = twoSessionGraph()

    const statuses = new Map([
      ['workflow:default:board:a', 'open'],
      ['task:default:board:a', 'blocked'],
      ['workflow:default:board:b', 'open'],
      ['task:default:board:b', 'completed']
    ])

    const filtered = visibleLiveGraph(
      {
        ...activeAndSettled,
        nodes: activeAndSettled.nodes.map(node => ({
          ...node,
          status: statuses.get(node.id) ?? node.status
        }))
      },
      {
        activeOnly: true,
        enabledKinds: new Set(DEFAULT_LIVE_GRAPH_VIEW_STATE.enabledKinds),
        focusDepth: 'all',
        orphans: true,
        search: ''
      }
    )

    expect(filtered.nodes.map(node => node.label).sort()).toEqual(['Session A', 'Task A', 'Workflow A'])
    expect(filtered.edges.map(edge => edge.id).sort()).toEqual(['edge:session-a-workflow', 'edge:workflow-a-task'])
  })

  it('filters node kinds before classifying filtered nodes as orphans', () => {
    const connected = visibleLiveGraph(graph, {
      enabledKinds: new Set(['workflow', 'task']),
      focusDepth: 1,
      orphans: false,
      search: ''
    })

    const hiddenOrphans = visibleLiveGraph(graph, {
      enabledKinds: new Set(['artifact']),
      focusDepth: 1,
      orphans: false,
      search: ''
    })

    const shownOrphans = visibleLiveGraph(graph, {
      enabledKinds: new Set(['artifact']),
      focusDepth: 1,
      orphans: true,
      search: ''
    })

    expect(connected.nodes.map(node => node.kind)).toEqual(['task', 'workflow'])
    expect(connected.edges.map(edge => edge.id)).toEqual(['edge:workflow-task'])
    expect(hiddenOrphans).toEqual({ edges: [], nodes: [] })
    expect(shownOrphans.nodes.map(node => node.label).sort()).toEqual(['Detached note', 'Verification report'])
    expect(shownOrphans.edges).toEqual([])
  })

  it('traverses incoming and outgoing links from the selected node', () => {
    const selectedWorkflow = visibleLiveGraph(graph, {
      enabledKinds: new Set(DEFAULT_LIVE_GRAPH_VIEW_STATE.enabledKinds),
      focusDepth: 1,
      focusId: 'workflow:default:board:wf',
      orphans: false,
      search: ''
    })

    const selectedTask = visibleLiveGraph(graph, {
      enabledKinds: new Set(DEFAULT_LIVE_GRAPH_VIEW_STATE.enabledKinds),
      focusDepth: 1,
      focusId: 'task:default:board:task',
      orphans: false,
      search: ''
    })

    expect(selectedWorkflow.nodes.map(node => node.kind).sort()).toEqual(['task', 'workflow'])
    expect(selectedTask.nodes.map(node => node.kind).sort()).toEqual(['agent', 'artifact', 'task', 'workflow'])
  })

  it('shows the complete reduced task DAG at one hop when focusing a workflow', () => {
    const { graph, ids } = hierarchicalTaskGraph()
    const otherWorkflowId = 'workflow:default:board:other'
    const otherTaskId = 'task:default:board:other'

    const hierarchy: LiveGraphSnapshot = {
      ...graph,
      edges: [
        ...graph.edges,
        { id: 'edge:tree-session-other', kind: 'contains', sourceId: ids.session, targetId: otherWorkflowId },
        { id: 'edge:tree-other-task', kind: 'contains', sourceId: otherWorkflowId, targetId: otherTaskId },
        { id: 'edge:tree-cross-workflow', kind: 'depends_on', sourceId: otherTaskId, targetId: ids.childTask }
      ],
      nodes: [
        ...graph.nodes,
        {
          board: 'board',
          entityId: 'other',
          id: otherWorkflowId,
          kind: 'workflow',
          label: 'Other workflow',
          workflowId: 'other'
        },
        {
          board: 'board',
          entityId: 'other',
          id: otherTaskId,
          kind: 'task',
          label: 'Other task',
          workflowId: 'other'
        }
      ]
    }

    const options = {
      enabledKinds: new Set(DEFAULT_LIVE_GRAPH_VIEW_STATE.enabledKinds),
      focusId: ids.workflow,
      orphans: false,
      search: ''
    }

    const depthOne = visibleLiveGraph(hierarchy, { ...options, focusDepth: 1 })
    const depthTwo = visibleLiveGraph(hierarchy, { ...options, focusDepth: 2 })
    const all = visibleLiveGraph(hierarchy, { ...options, focusDepth: 'all' })

    expect(new Set(depthOne.nodes.map(node => node.id))).toEqual(new Set([ids.workflow, ids.rootTask, ids.childTask]))
    expect(new Set(depthOne.edges.map(edge => edge.id))).toEqual(
      new Set(['edge:tree-workflow-root', 'edge:tree-child-depends-root'])
    )
    expect(new Set(depthTwo.nodes.map(node => node.id))).toEqual(
      new Set([ids.workflow, ids.rootTask, ids.childTask, ids.agent])
    )
    expect(depthTwo.nodes.some(node => node.id === ids.artifact)).toBe(false)
    expect(new Set(all.nodes.map(node => node.id))).toEqual(new Set(hierarchy.nodes.map(node => node.id)))
  })

  it('shows orphans only when requested and applies search after graph depth', () => {
    const withoutOrphans = visibleLiveGraph(graph, {
      enabledKinds: new Set(DEFAULT_LIVE_GRAPH_VIEW_STATE.enabledKinds),
      focusDepth: 'all',
      orphans: false,
      search: ''
    })

    const matchingOrphan = visibleLiveGraph(graph, {
      enabledKinds: new Set(DEFAULT_LIVE_GRAPH_VIEW_STATE.enabledKinds),
      focusDepth: 'all',
      orphans: true,
      search: 'Detached'
    })

    expect(withoutOrphans.nodes.some(node => node.id.includes('orphan'))).toBe(false)
    expect(matchingOrphan.nodes.map(node => node.label)).toEqual(['Detached note'])
  })

  it('settles deterministically regardless of node and edge input order', () => {
    const settings = { centerForce: 72, linkDistance: 112, linkForce: 42, repelForce: 62 }
    const first = settledLiveGraphLayout(graph.nodes, graph.edges, settings, graph.rootId)
    const repeated = settledLiveGraphLayout(graph.nodes, graph.edges, settings, graph.rootId)

    const reversed = settledLiveGraphLayout(
      [...graph.nodes].reverse(),
      [...graph.edges].reverse(),
      settings,
      graph.rootId
    )

    const positions = (layout: ReturnType<typeof settledLiveGraphLayout>) =>
      new Map(layout.map(node => [node.id, { x: node.x, y: node.y }]))

    expect(repeated).toEqual(first)
    expect(positions(reversed)).toEqual(positions(first))

    const camera = fitLiveGraphCamera(first, { height: 600, width: 900 })

    expect(camera.scale).toBeGreaterThan(0)
    expect(Number.isFinite(camera.x)).toBe(true)
    expect(Number.isFinite(camera.y)).toBe(true)
  })

  it('keeps attachment links tighter than hierarchy links and task links', () => {
    const settings = {
      centerForce: DEFAULT_LIVE_GRAPH_VIEW_STATE.centerForce,
      linkDistance: DEFAULT_LIVE_GRAPH_VIEW_STATE.linkDistance,
      linkForce: DEFAULT_LIVE_GRAPH_VIEW_STATE.linkForce,
      repelForce: DEFAULT_LIVE_GRAPH_VIEW_STATE.repelForce
    }

    const gaps = Object.fromEntries(
      (['contains', 'delegated_to', 'depends_on', 'produced'] as const).map(kind => {
        const pair = oneHopLinkGraph(kind)
        const layout = settledLiveGraphLayout(pair.nodes, pair.edges, settings)

        return [kind, nodeSurfaceGap(pair, layout, pair.nodes[0]!.id, pair.nodes[1]!.id)]
      })
    )

    expect(gaps.delegated_to).toBeLessThan(gaps.contains)
    expect(gaps.produced).toBeCloseTo(gaps.delegated_to, 1)
    expect(gaps.contains).toBeLessThan(gaps.depends_on)
  })

  it('keeps linked nodes closer than unlinked peers', () => {
    const snapshot = forceStarGraph()

    const layout = settledLiveGraphLayout(snapshot.nodes, snapshot.edges, {
      centerForce: 72,
      linkDistance: 112,
      linkForce: 42,
      repelForce: 62
    })

    const hubId = 'workflow:force:hub'
    const leafIds = snapshot.nodes.map(node => node.id).filter(id => id !== hubId)
    const linkedDistances = leafIds.map(id => nodeCenterDistance(layout, hubId, id))

    const unlinkedDistances = leafIds.flatMap((leftId, leftIndex) =>
      leafIds.slice(leftIndex + 1).map(rightId => nodeCenterDistance(layout, leftId, rightId))
    )

    const mean = (values: readonly number[]) => values.reduce((sum, value) => sum + value, 0) / values.length

    expect(mean(linkedDistances)).toBeLessThan(mean(unlinkedDistances))
  })

  it('keeps variable-radius nodes collision-clear in a dense connected graph', () => {
    const snapshot = denseCollisionGraph(80)

    const layout = settledLiveGraphLayout(snapshot.nodes, snapshot.edges, {
      centerForce: DEFAULT_LIVE_GRAPH_VIEW_STATE.centerForce,
      linkDistance: DEFAULT_LIVE_GRAPH_VIEW_STATE.linkDistance,
      linkForce: DEFAULT_LIVE_GRAPH_VIEW_STATE.linkForce,
      repelForce: DEFAULT_LIVE_GRAPH_VIEW_STATE.repelForce
    })

    const minimum = minimumVisibleNodeClearance(snapshot, layout)

    expect(layout).toHaveLength(snapshot.nodes.length)
    expect(layout.every(node => Number.isFinite(node.x) && Number.isFinite(node.y))).toBe(true)
    expect(minimum.clearance, `${minimum.left} overlaps ${minimum.right}`).toBeGreaterThanOrEqual(-0.1)
  })
  it('fits an oversized graph below the old zoom floor with padded bounds intact', () => {
    const nodes = [
      {
        id: 'task:left',
        node: { entityId: 'left', id: 'task:left', kind: 'task' as const, label: 'Left' },
        x: -10_000,
        y: -4_000
      },
      {
        id: 'task:right',
        node: { entityId: 'right', id: 'task:right', kind: 'task' as const, label: 'Right' },
        x: 10_000,
        y: 4_000
      }
    ]

    const radiusById = new Map([
      ['task:left', 10],
      ['task:right', 10]
    ])

    const viewport = { height: 600, width: 900 }
    const camera = fitLiveGraphCamera(nodes, viewport, radiusById)
    const projectX = (x: number) => camera.x + x * camera.scale
    const projectY = (y: number) => camera.y + y * camera.scale

    expect(camera.scale).toBeCloseTo(852 / 20_088, 8)
    expect(camera.scale).toBeLessThan(0.2)
    expect(projectX(-10_044)).toBeCloseTo(24, 6)
    expect(projectX(10_044)).toBeCloseTo(876, 6)
    expect(projectY(-4_044)).toBeGreaterThanOrEqual(24)
    expect(projectY(4_044)).toBeLessThanOrEqual(576)

    expect(fitLiveGraphCamera([nodes[0]!], viewport, radiusById).scale).toBe(2)
  })

  it('keeps disconnected components distinct and every visible radius collision-clear', () => {
    const sessions = twoSessionGraph()

    const layout = settledLiveGraphLayout(
      sessions.nodes,
      sessions.edges,
      { centerForce: 72, linkDistance: 112, linkForce: 42, repelForce: 62 },
      sessions.rootId
    )

    const analysis = analyzeLiveGraphTopology(sessions.nodes, sessions.edges)
    const positions = new Map(layout.map(node => [node.id, node]))

    const components = analysis.components.filter(component =>
      component.some(id => (analysis.degreeById.get(id) ?? 0) > 0)
    )

    const centroid = (ids: readonly string[]) => ({
      x: ids.reduce((sum, id) => sum + positions.get(id)!.x, 0) / ids.length,
      y: ids.reduce((sum, id) => sum + positions.get(id)!.y, 0) / ids.length
    })

    const left = centroid(components[0]!)
    const right = centroid(components[1]!)
    const leftBounds = componentBounds(sessions, layout, components[0]!)
    const rightBounds = componentBounds(sessions, layout, components[1]!)

    const componentSurfaceGap = Math.max(
      leftBounds.left - rightBounds.right,
      rightBounds.left - leftBounds.right,
      leftBounds.top - rightBounds.bottom,
      rightBounds.top - leftBounds.bottom
    )

    const minimum = minimumVisibleNodeClearance(sessions, layout)
    const taskDiameter = liveGraphNodeRadius(sessions.nodes.find(node => node.kind === 'task')!, 0) * 2

    expect(components).toHaveLength(2)
    expect(Math.hypot(left.x - right.x, left.y - right.y)).toBeGreaterThan(taskDiameter)
    expect(componentSurfaceGap).toBeGreaterThanOrEqual(4)
    expect(minimum.clearance, `${minimum.left} overlaps ${minimum.right}`).toBeGreaterThanOrEqual(-0.1)
  })
  it('makes every connected force control materially reshape the graph', () => {
    const centerGraph: LiveGraphSnapshot = {
      edges: [
        {
          id: 'edge:center:a-bridge',
          kind: 'delegated_to',
          sourceId: 'task:center:a',
          targetId: 'agent:center:bridge'
        },
        { id: 'edge:center:b-bridge', kind: 'delegated_to', sourceId: 'task:center:b', targetId: 'agent:center:bridge' }
      ],
      nodes: [
        { entityId: 'a', id: 'task:center:a', kind: 'task', label: 'Center root A' },
        { entityId: 'b', id: 'task:center:b', kind: 'task', label: 'Center root B' },
        { entityId: 'bridge', id: 'agent:center:bridge', kind: 'agent', label: 'Center bridge' }
      ]
    }

    const siblingIds = Array.from({ length: 6 }, (_, index) => `task:force-control:sibling:${index}`)

    const forceGraph: LiveGraphSnapshot = {
      edges: siblingIds.flatMap((taskId, index) => [
        {
          id: `edge:force-control:hub-task:${index}`,
          kind: 'contains' as const,
          sourceId: 'workflow:force-control:hub',
          targetId: taskId
        },
        {
          id: `edge:force-control:task-leaf:${index}`,
          kind: 'produced' as const,
          sourceId: taskId,
          targetId: `artifact:force-control:leaf:${index}`
        }
      ]),
      nodes: [
        { entityId: 'hub', id: 'workflow:force-control:hub', kind: 'workflow', label: 'Force control hub' },
        ...siblingIds.flatMap((taskId, index) => [
          { entityId: `task-${index}`, id: taskId, kind: 'task' as const, label: `Force task ${index}` },
          {
            entityId: `leaf-${index}`,
            id: `artifact:force-control:leaf:${index}`,
            kind: 'artifact' as const,
            label: `Force leaf ${index}`
          }
        ])
      ]
    }

    const linkGraph: LiveGraphSnapshot = {
      edges: [
        {
          id: 'edge:force-link:parent-hub',
          kind: 'contains',
          sourceId: 'task:force-link:parent',
          targetId: 'task:force-link:hub'
        },
        {
          id: 'edge:force-link:hub-leaf',
          kind: 'produced',
          sourceId: 'task:force-link:hub',
          targetId: 'artifact:force-link:leaf'
        }
      ],
      nodes: [
        { entityId: 'parent', id: 'task:force-link:parent', kind: 'task', label: 'Force link parent' },
        { entityId: 'hub', id: 'task:force-link:hub', kind: 'task', label: 'Force link hub' },
        { entityId: 'leaf', id: 'artifact:force-link:leaf', kind: 'artifact', label: 'Force link leaf' }
      ]
    }

    const settings = { centerForce: 72, linkDistance: 112, linkForce: 42, repelForce: 62 }

    const meanPairwiseDistance = (layout: ReturnType<typeof settledLiveGraphLayout>) => {
      let distance = 0
      let pairs = 0

      for (let left = 0; left < layout.length; left += 1) {
        for (let right = left + 1; right < layout.length; right += 1) {
          distance += Math.hypot(layout[left]!.x - layout[right]!.x, layout[left]!.y - layout[right]!.y)
          pairs += 1
        }
      }

      return distance / pairs
    }

    const meanDistanceAmong = (layout: ReturnType<typeof settledLiveGraphLayout>, ids: readonly string[]) => {
      const positions = new Map(layout.map(node => [node.id, node]))
      let distance = 0
      let pairs = 0

      for (let left = 0; left < ids.length; left += 1) {
        for (let right = left + 1; right < ids.length; right += 1) {
          const leftPosition = positions.get(ids[left]!)!
          const rightPosition = positions.get(ids[right]!)!

          distance += Math.hypot(leftPosition.x - rightPosition.x, leftPosition.y - rightPosition.y)
          pairs += 1
        }
      }

      return distance / pairs
    }

    const noCenter = settledLiveGraphLayout(centerGraph.nodes, centerGraph.edges, { ...settings, centerForce: 0 })

    const strongCenter = settledLiveGraphLayout(centerGraph.nodes, centerGraph.edges, {
      ...settings,
      centerForce: 100
    })

    const lowRepel = meanDistanceAmong(
      settledLiveGraphLayout(forceGraph.nodes, forceGraph.edges, { ...settings, repelForce: 10 }),
      siblingIds
    )

    const highRepel = meanDistanceAmong(
      settledLiveGraphLayout(forceGraph.nodes, forceGraph.edges, { ...settings, repelForce: 180 }),
      siblingIds
    )

    const weakLinks = nodeSurfaceGap(
      linkGraph,
      settledLiveGraphLayout(linkGraph.nodes, linkGraph.edges, { ...settings, linkDistance: 220, linkForce: 0 }),
      'task:force-link:parent',
      'task:force-link:hub'
    )

    const strongLinks = nodeSurfaceGap(
      linkGraph,
      settledLiveGraphLayout(linkGraph.nodes, linkGraph.edges, { ...settings, linkDistance: 220, linkForce: 100 }),
      'task:force-link:parent',
      'task:force-link:hub'
    )

    const shortLinks = nodeSurfaceGap(
      linkGraph,
      settledLiveGraphLayout(linkGraph.nodes, linkGraph.edges, { ...settings, linkDistance: 48, linkForce: 100 }),
      'task:force-link:parent',
      'task:force-link:hub'
    )

    const longLinks = nodeSurfaceGap(
      linkGraph,
      settledLiveGraphLayout(linkGraph.nodes, linkGraph.edges, { ...settings, linkDistance: 220, linkForce: 100 }),
      'task:force-link:parent',
      'task:force-link:hub'
    )

    expect(meanPairwiseDistance(strongCenter)).toBeLessThan(meanPairwiseDistance(noCenter) * 0.97)
    expect(highRepel).toBeGreaterThan(lowRepel * 1.05)
    expect(Math.abs(strongLinks - weakLinks)).toBeGreaterThan(weakLinks * 0.1)
    expect(longLinks).toBeGreaterThan(shortLinks * 1.1)
  })

  it('bounds wake heat before the simulation enters its finite cooling pass', () => {
    expect(liveGraphSimulationHeat(120)).toEqual({
      activeFrameInterval: 16,
      alphaTarget: 0.025
    })
    expect(liveGraphSimulationHeat(2_517)).toEqual({
      activeFrameInterval: 120,
      alphaTarget: 0.006
    })
    expect(liveGraphReheatAlpha(799, 0.42)).toBe(0.42)
    expect(liveGraphReheatAlpha(800, 0.42)).toBe(0.04)
    expect(liveGraphReheatAlpha(2_517, 0.16)).toBe(0.04)
    expect(liveGraphStartupHeat(159)).toEqual({ alpha: 0, duration: 0 })
    expect(liveGraphStartupHeat(160)).toEqual({ alpha: 0.12, duration: 1_500 })
    expect(liveGraphStartupHeat(299)).toEqual({ alpha: 0.12, duration: 1_500 })
    expect(liveGraphStartupHeat(300)).toEqual({ alpha: 0.12, duration: 1_500 })
    expect(liveGraphStartupHeat(799)).toEqual({ alpha: 0.12, duration: 1_500 })
    expect(liveGraphStartupHeat(800)).toEqual({ alpha: 0.22, duration: 2_500 })
    expect(liveGraphStartupHeat(2_517)).toEqual({ alpha: 0.22, duration: 2_500 })
  })

  it('caps cooling work at the dense-graph boundary', () => {
    expect(liveGraphCoolingTickBudget(0)).toBe(48)
    expect(liveGraphCoolingTickBudget(799)).toBe(48)
    expect(liveGraphCoolingTickBudget(800)).toBe(24)
    expect(liveGraphCoolingTickBudget(2_517)).toBe(24)
  })

  it('decays active heat to a genuinely cold alpha within the cooling budget', () => {
    const localDecay = liveGraphCoolingAlphaDecay(0.42, 48, 0.0228)
    const denseDecay = liveGraphCoolingAlphaDecay(0.22, 24, 0.28)

    expect(0.42 * Math.pow(1 - localDecay, 48)).toBeLessThanOrEqual(0.002_001)
    expect(0.22 * Math.pow(1 - denseDecay, 24)).toBeLessThanOrEqual(0.002_001)
    expect(localDecay).toBeGreaterThan(0.0228)
    expect(denseDecay).toBe(0.28)
  })

  it('sleeps only when RMS and peak movement are both within the node-relative boundary', () => {
    expect(liveGraphMotionSettled(0.1, 0.2, 20)).toBe(true)
    expect(liveGraphMotionSettled(0.100_001, 0.2, 20)).toBe(false)
    expect(liveGraphMotionSettled(0.1, 0.200_001, 20)).toBe(false)
    expect(liveGraphMotionSettled(0.005, 0.02, 0)).toBe(true)
    expect(liveGraphMotionSettled(0.005_001, 0.02, 0)).toBe(false)
    expect(liveGraphMotionSettled(0.025, 0.05, 20, 4)).toBe(true)
    expect(liveGraphMotionSettled(0.025_001, 0.05, 20, 4)).toBe(false)
    expect(liveGraphMotionSettled(0.025, 0.050_001, 20, 4)).toBe(false)
  })

  it('keeps the settled topology stable across status and label refreshes', () => {
    const refreshedNodes = graph.nodes.map(node => ({
      ...node,
      label: node.kind === 'task' ? 'Verify packaged app' : node.label,
      status: node.kind === 'task' ? 'blocked' : node.status
    }))

    const rewiredEdges = graph.edges.map(edge =>
      edge.id === 'edge:workflow-task' ? { ...edge, sourceId: 'session:default:root' } : edge
    )

    const rekindedEdges = graph.edges.map(edge =>
      edge.id === 'edge:workflow-task' ? { ...edge, kind: 'depends_on' as const } : edge
    )

    expect(liveGraphTopologyKey(refreshedNodes, graph.edges)).toBe(liveGraphTopologyKey(graph.nodes, graph.edges))
    expect(liveGraphTopologyKey(graph.nodes, rewiredEdges)).not.toBe(liveGraphTopologyKey(graph.nodes, graph.edges))
    expect(liveGraphTopologyKey(graph.nodes, rekindedEdges)).not.toBe(liveGraphTopologyKey(graph.nodes, graph.edges))
  })

  it('keeps semantic node sizes stable when a filtered view hides part of the graph', () => {
    const reach = liveGraphSemanticReachCounts(graph.nodes, graph.edges)
    const descendantHubIds = liveGraphDescendantHubIds(graph.nodes, graph.edges)
    const fullRadii = liveGraphNodeRadii(graph.nodes, reach, 100, descendantHubIds)
    const filteredNodes = graph.nodes.filter(node => node.kind === 'task' || node.kind === 'agent')
    const filteredRadii = liveGraphNodeRadii(filteredNodes, reach, 100, descendantHubIds)

    expect(filteredRadii).toEqual(new Map(filteredNodes.map(node => [node.id, fullRadii.get(node.id)])))
  })

  it('sizes only session, workflow, and workflow-root task hubs by descendants', () => {
    const reach = liveGraphSemanticReachCounts(graph.nodes, graph.edges)
    const descendantHubIds = liveGraphDescendantHubIds(graph.nodes, graph.edges)
    const radii = liveGraphNodeRadii(graph.nodes, reach, 100, descendantHubIds)
    const session = graph.nodes.find(node => node.kind === 'session')!
    const workflow = graph.nodes.find(node => node.kind === 'workflow')!
    const rootTask = graph.nodes.find(node => node.kind === 'task')!
    const agent = graph.nodes.find(node => node.kind === 'agent')!
    const artifact = graph.nodes.find(node => node.kind === 'artifact')!

    expect(descendantHubIds).toEqual(new Set([session.id, workflow.id, rootTask.id]))
    expect(radii.get(session.id)).toBeGreaterThan(radii.get(workflow.id)!)
    expect(radii.get(workflow.id)).toBeGreaterThan(radii.get(rootTask.id)!)
    expect(radii.get(rootTask.id)).toBeGreaterThan(radii.get(agent.id)!)
    expect(radii.get(agent.id)).toBe(radii.get(artifact.id))

    const nestedTask = { entityId: 'nested', id: 'task:nested', kind: 'task' as const, label: 'Nested task' }
    expect(liveGraphNodeRadii([nestedTask], new Map([[nestedTask.id, 600]]), 100, new Set()).get(nestedTask.id)).toBe(
      liveGraphNodeRadius(nestedTask, 0)
    )
  })

  it('scales every semantic node radius uniformly without flattening hubs', () => {
    const nodes = graph.nodes
    const reach = liveGraphSemanticReachCounts(graph.nodes, graph.edges)
    const descendantHubIds = liveGraphDescendantHubIds(graph.nodes, graph.edges)
    const compact = liveGraphNodeRadii(nodes, reach, 50, descendantHubIds)
    const standard = liveGraphNodeRadii(nodes, reach, 100, descendantHubIds)
    const large = liveGraphNodeRadii(nodes, reach, 200, descendantHubIds)

    for (const node of nodes) {
      expect(compact.get(node.id)).toBeCloseTo(standard.get(node.id)! * 0.5, 8)
      expect(large.get(node.id)).toBeCloseTo(standard.get(node.id)! * 2, 8)
    }

    const hubId = nodes.find(node => node.kind === 'session')!.id
    const leafId = nodes.find(node => node.kind === 'agent')!.id

    expect(large.get(hubId)! / large.get(leafId)!).toBeCloseTo(standard.get(hubId)! / standard.get(leafId)!, 8)
    expect(large.get(hubId)).toBeGreaterThan(large.get(leafId)!)
  })

  it('trims graph edges to the source and target boundaries', () => {
    expect(trimLiveGraphEdge({ x: 0, y: 0 }, { x: 100, y: 0 }, 10, 20)).toEqual({
      x1: 12,
      x2: 78,
      y1: 0,
      y2: 0
    })
  })

  it('builds one trimmed compound path and skips edges with missing endpoints', () => {
    const edges: LiveGraphSnapshot['edges'] = [
      { id: 'edge:a-b', kind: 'contains', sourceId: 'task:a', targetId: 'task:b' },
      { id: 'edge:b-c', kind: 'contains', sourceId: 'task:b', targetId: 'task:c' },
      { id: 'edge:missing', kind: 'contains', sourceId: 'task:a', targetId: 'task:missing' }
    ]

    const path = liveGraphEdgePath(
      edges,
      new Map([
        ['task:a', { x: 0, y: 0 }],
        ['task:b', { x: 20, y: 0 }],
        ['task:c', { x: 20, y: 20 }]
      ]),
      new Map([
        ['task:a', 2],
        ['task:b', 2],
        ['task:c', 2]
      ]),
      0
    )

    expect(path).toBe('M2,0L18,0M20,2L20,18')
    expect(path).not.toContain('NaN')
  })

  it('maps pointer coordinates through canvas offset, pan, and zoom', () => {
    expect(clientToLiveGraphPoint(110, 70, { left: 10, top: 20 }, { scale: 2, x: 30, y: -10 })).toEqual({
      x: 35,
      y: 30
    })
  })

  it('sanitizes persisted controls and camera bounds', () => {
    expect(
      normalizeLiveGraphViewState({
        activeOnly: true,
        arrows: false,
        camera: { scale: 99, x: 12, y: -4 },
        enabledKinds: ['task', 'not-a-kind'],
        focusDepth: 1,
        linkDistance: 999,
        linkForce: 999,
        nodeSize: 999,
        search: 'verify',
        textFadeThreshold: 999
      })
    ).toMatchObject({
      activeOnly: true,
      arrows: false,
      camera: { scale: 4, x: 12, y: -4 },
      enabledKinds: ['task'],
      focusDepth: 1,
      linkDistance: 220,
      linkForce: 100,
      nodeSize: 200,
      search: 'verify',
      textFadeThreshold: 200
    })

    expect(normalizeLiveGraphViewState({ camera: { scale: 0.03, x: 1, y: 2 } }).camera).toEqual({
      scale: 0.03,
      x: 1,
      y: 2
    })

    expect(normalizeLiveGraphViewState({ camera: { scale: 0, x: 1, y: 2 } }).camera.scale).toBe(LIVE_GRAPH_MIN_SCALE)
    expect(normalizeLiveGraphViewState({ nodeSize: 1 }).nodeSize).toBe(50)
    expect(normalizeLiveGraphViewState({ nodeSize: Number.NaN }).nodeSize).toBe(100)
    expect(normalizeLiveGraphViewState({}).nodeSize).toBe(100)
    expect(normalizeLiveGraphViewState({}).activeOnly).toBe(false)
    expect(normalizeLiveGraphViewState({}).textFadeThreshold).toBe(200)
  })

  it('normalizes backend status aliases consistently', () => {
    expect(normalizeLiveGraphStatus('in_progress')).toBe('running')
    expect(normalizeLiveGraphStatus('success')).toBe('completed')
    expect(normalizeLiveGraphStatus('error')).toBe('failed')
    expect(normalizeLiveGraphStatus('review_required')).toBe('blocked')
  })
})

describe('LiveGraphCanvas', () => {
  it('stops requesting frames after settling and wakes when a force changes', async () => {
    const disconnected = disconnectedGraph(12)
    const originalRequestAnimationFrame = globalThis.requestAnimationFrame
    let frameRequests = 0

    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      frameRequests += 1

      return globalThis.window.setTimeout(() => callback(performance.now()), 16)
    })

    const { container, unmount } = render(
      <LiveGraphCanvas
        autoFit={false}
        graph={disconnected}
        initialState={{ ...DEFAULT_LIVE_GRAPH_VIEW_STATE, focusDepth: 'all', orphans: true }}
      />
    )

    try {
      await new Promise(resolve => window.setTimeout(resolve, 1_200))
      const settledFrameRequests = frameRequests
      const settledRoot = container.querySelector('[data-live-graph-simulation-state="settled"]')

      expect(settledRoot).toBeTruthy()
      expect(['balanced', 'capped']).toContain(settledRoot?.getAttribute('data-live-graph-sleep-reason'))
      await new Promise(resolve => window.setTimeout(resolve, 160))
      expect(frameRequests).toBe(settledFrameRequests)

      fireEvent.click(screen.getByRole('button', { name: 'Graph settings' }))
      const settings = await screen.findByRole('dialog', { name: 'Graph settings' })
      const repel = within(settings).getByRole('slider', { name: 'Repel force' })

      fireEvent.input(repel, { target: { value: '180' } })

      await waitFor(() => expect(frameRequests).toBeGreaterThan(settledFrameRequests))
      expect(container.querySelector('[data-live-graph-simulation-state="running"]')).toBeTruthy()
    } finally {
      unmount()
      vi.stubGlobal('requestAnimationFrame', originalRequestAnimationFrame)
    }
  }, 10_000)

  it('keeps the canvas clean until graph settings are opened', async () => {
    render(<LiveGraphCanvas graph={graph} />)

    expect(screen.queryByRole('textbox', { name: 'Search graph…' })).toBeNull()
    expect(screen.queryByRole('slider', { name: 'Center force' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Graph settings' }))

    const settings = await screen.findByRole('dialog', { name: 'Graph settings' })

    expect(within(settings).getByRole('textbox', { name: 'Search graph…' })).toBeTruthy()
    expect(within(settings).getAllByRole('switch')).toHaveLength(10)
    expect(within(settings).getByRole('switch', { name: 'Active only' })).toBeTruthy()
    expect(within(settings).getByRole('slider', { name: 'Center force' })).toBeTruthy()
    expect(within(settings).getByRole('slider', { name: 'Repel force' })).toBeTruthy()
    expect(within(settings).getByRole('slider', { name: 'Link force' })).toBeTruthy()
    expect(within(settings).getByRole('slider', { name: 'Link distance' })).toBeTruthy()
    expect(within(settings).getByRole('button', { name: 'Animate' })).toBeTruthy()
  })

  it('previews force changes immediately and persists once on release', async () => {
    const onStateChange = vi.fn()

    render(<LiveGraphCanvas autoFit={false} graph={graph} onStateChange={onStateChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'Graph settings' }))

    const settings = await screen.findByRole('dialog', { name: 'Graph settings' })
    const center = within(settings).getByRole('slider', { name: 'Center force' }) as HTMLInputElement
    const linkForce = within(settings).getByRole('slider', { name: 'Link force' }) as HTMLInputElement
    const callsBeforePreview = onStateChange.mock.calls.length

    fireEvent.input(center, { target: { value: '100' } })
    fireEvent.input(linkForce, { target: { value: '80' } })

    expect(center.value).toBe('100')
    expect(linkForce.value).toBe('80')
    expect(onStateChange).toHaveBeenCalledTimes(callsBeforePreview)

    fireEvent.pointerUp(linkForce)

    expect(onStateChange).toHaveBeenCalledTimes(callsBeforePreview + 1)
    expect(onStateChange.mock.lastCall?.[0]).toMatchObject({ centerForce: 100, linkForce: 80 })
  })

  it('keeps visible node bodies stable while force controls reheat the graph', async () => {
    const matchMedia = vi.mocked(window.matchMedia)
    const originalMatchMedia = matchMedia.getMockImplementation()
    const onStateChange = vi.fn()

    matchMedia.mockImplementation(
      () =>
        ({
          addEventListener: vi.fn(),
          dispatchEvent: vi.fn(),
          matches: true,
          media: '(prefers-reduced-motion: reduce)',
          onchange: null,
          removeEventListener: vi.fn()
        }) as unknown as MediaQueryList
    )

    try {
      const sessions = twoSessionGraph()

      const { container } = render(
        <LiveGraphCanvas
          autoFit={false}
          graph={sessions}
          initialState={{ ...DEFAULT_LIVE_GRAPH_VIEW_STATE, focusDepth: 'all' }}
          onStateChange={onStateChange}
        />
      )

      fireEvent.click(screen.getByRole('button', { name: 'Graph settings' }))
      const settings = await screen.findByRole('dialog', { name: 'Graph settings' })
      const center = within(settings).getByRole('slider', { name: 'Center force' })
      const linkDistance = within(settings).getByRole('slider', { name: 'Link distance' })
      const nodeIds = sessions.nodes.map(node => node.id)
      const clearance = () => renderedMinimumNodeClearance(container, nodeIds)

      expect(clearance()).toBeGreaterThanOrEqual(-1)

      for (let index = 0; index < 12; index += 1) {
        fireEvent.input(center, { target: { value: index % 2 === 0 ? '100' : '99' } })
        fireEvent.input(linkDistance, { target: { value: index % 2 === 0 ? '220' : '219' } })
      }

      fireEvent.input(center, { target: { value: '100' } })
      fireEvent.input(linkDistance, { target: { value: '220' } })
      fireEvent.pointerUp(linkDistance)

      expect(clearance()).toBeGreaterThanOrEqual(-1.5)
      expect(
        sessions.nodes.every(node => {
          const position = graphNodePosition(container.querySelector(`[data-live-graph-node-id="${node.id}"]`)!)

          return Number.isFinite(position.x) && Number.isFinite(position.y)
        })
      ).toBe(true)
      expect(onStateChange.mock.lastCall?.[0]).toMatchObject({ centerForce: 100, linkDistance: 220 })
    } finally {
      cleanup()

      if (originalMatchMedia) {
        matchMedia.mockImplementation(originalMatchMedia)
      }
    }
  })

  it('fades local links at rest and fully reveals the hovered node neighborhood', async () => {
    const { container } = render(
      <LiveGraphCanvas graph={graph} initialState={{ ...DEFAULT_LIVE_GRAPH_VIEW_STATE, focusDepth: 'all' }} />
    )

    const workflow = await screen.findByRole('button', { name: /Workflow: Correct artifact/ })

    const incident = container.querySelector(
      '[data-live-graph-edge-source="session:default:root"][data-live-graph-edge-target="workflow:default:board:wf"]'
    )!

    const unrelated = container.querySelector(
      '[data-live-graph-edge-source="task:default:board:task"][data-live-graph-edge-target="agent:default:board:task:worker"]'
    )!

    expect(incident.getAttribute('opacity')).toBe('0.48')
    expect(unrelated.getAttribute('opacity')).toBe('0.48')

    fireEvent.pointerEnter(workflow)

    expect(incident.getAttribute('opacity')).toBe('1')
    expect(incident.getAttribute('stroke')).toBe('var(--ui-text-secondary)')
    expect(unrelated.getAttribute('opacity')).toBe('0.07')

    fireEvent.pointerLeave(workflow)

    expect(incident.getAttribute('opacity')).toBe('0.48')
    expect(unrelated.getAttribute('opacity')).toBe('0.48')
  })

  it('renders all node kinds as circular spheres with unique type palettes', async () => {
    const { container } = render(
      <LiveGraphCanvas graph={sixKindGraph} initialState={{ ...DEFAULT_LIVE_GRAPH_VIEW_STATE, focusDepth: 'all' }} />
    )

    await screen.findByRole('button', { name: /Artifact: Artifact/ })

    const expectedIcons: Record<(typeof LIVE_GRAPH_KINDS)[number], string> = {
      agent: 'hubot',
      artifact: 'file',
      project: 'folder',
      session: 'comment-discussion',
      task: 'checklist',
      workflow: 'type-hierarchy'
    }

    const iconGlyphs = new Set<string>()

    const fills = LIVE_GRAPH_KINDS.map(kind => {
      const node = container.querySelector(`[data-live-graph-node-kind="${kind}"]`)
      const body = node?.querySelector('[data-live-graph-node-body]')
      const icon = node?.querySelector(`[data-live-graph-node-kind-icon="${expectedIcons[kind]}"]`)

      expect(node).not.toBeNull()
      expect(body?.tagName.toLowerCase()).toBe('circle')
      expect(Number(body?.getAttribute('r'))).toBeGreaterThan(0)
      expect(body?.getAttribute('fill')).toBeTruthy()
      expect(body?.getAttribute('fill')).not.toBe('var(--ui-bg-editor)')
      expect(icon?.tagName.toLowerCase()).toBe('text')
      expect(icon?.getAttribute('aria-hidden')).toBe('true')
      expect(icon?.getAttribute('fill')).toBe('var(--ui-bg-editor)')
      expect(icon?.getAttribute('font-family')).toBe('codicon')
      expect(Number(icon?.getAttribute('font-size'))).toBeGreaterThan(0)
      iconGlyphs.add(icon?.textContent || '')

      return body?.getAttribute('fill')
    })

    expect(new Set(fills).size).toBe(LIVE_GRAPH_KINDS.length)
    expect(iconGlyphs.size).toBe(LIVE_GRAPH_KINDS.length)
  })

  it('keeps node type primary while active tasks get visible status markers', async () => {
    const { container } = render(
      <LiveGraphCanvas graph={sixKindGraph} initialState={{ ...DEFAULT_LIVE_GRAPH_VIEW_STATE, focusDepth: 'all' }} />
    )

    const runningTask = await screen.findByRole('button', { name: /Task: Running task/ })
    const blockedTask = await screen.findByRole('button', { name: /Task: Blocked task/ })
    const workflow = await screen.findByRole('button', { name: /Workflow: Workflow/ })
    const agent = await screen.findByRole('button', { name: /Agent: Agent/ })

    const bodyFill = (node: Element) => node.querySelector('[data-live-graph-node-body]')?.getAttribute('fill')
    const status = (node: Element) => node.querySelector('[data-live-graph-node-status-ring]')

    expect(bodyFill(runningTask)).toBe(bodyFill(blockedTask))
    expect(status(runningTask)?.getAttribute('stroke')).not.toBe(status(blockedTask)?.getAttribute('stroke'))
    expect(bodyFill(runningTask)).not.toBe(bodyFill(workflow))
    expect(status(runningTask)?.getAttribute('stroke')).toBe(status(workflow)?.getAttribute('stroke'))
    expect(status(runningTask)?.getAttribute('fill')).toBe('none')
    expect(runningTask.querySelector('[data-live-graph-active-halo]')).not.toBeNull()
    expect(runningTask.querySelector('[data-live-graph-node-running-marker]')).not.toBeNull()
    expect(blockedTask.querySelector('[data-live-graph-active-halo]')).not.toBeNull()
    expect(blockedTask.querySelector('[data-live-graph-node-blocked-marker]')).not.toBeNull()
    expect(agent.querySelector('[data-live-graph-node-work-marker]')).toBeNull()
    expect(Number(status(runningTask)?.getAttribute('r'))).toBeGreaterThan(
      Number(runningTask.querySelector('[data-live-graph-node-body]')?.getAttribute('r'))
    )

    fireEvent.click(agent)

    expect(agent.querySelector('[data-live-graph-node-selection]')?.tagName.toLowerCase()).toBe('circle')
    expect(container.querySelectorAll('[data-live-graph-node-selection]')).toHaveLength(1)
  })

  it('recedes settled task context when Active only is enabled', async () => {
    const completedTask: LiveGraphNode = {
      entityId: 'completed',
      id: 'task:completed',
      kind: 'task',
      label: 'Completed context',
      status: 'completed'
    }

    render(
      <LiveGraphCanvas
        graph={{
          ...sixKindGraph,
          edges: [
            ...sixKindGraph.edges,
            {
              id: 'edge:workflow-completed',
              kind: 'contains',
              sourceId: 'workflow:types',
              targetId: completedTask.id
            }
          ],
          nodes: [...sixKindGraph.nodes, completedTask]
        }}
        initialState={{ ...DEFAULT_LIVE_GRAPH_VIEW_STATE, activeOnly: true, focusDepth: 'all' }}
      />
    )

    const runningTask = await screen.findByRole('button', { name: /Task: Running task/ })
    const settledTask = await screen.findByRole('button', { name: /Task: Completed context/ })
    const workflow = await screen.findByRole('button', { name: /Workflow: Workflow/ })

    expect(runningTask.getAttribute('data-live-graph-node-work-state')).toBe('running')
    expect(runningTask.hasAttribute('data-live-graph-settled-context')).toBe(false)
    expect(settledTask.getAttribute('data-live-graph-node-work-state')).toBe('settled')
    expect(settledTask.getAttribute('data-live-graph-settled-context')).toBe('true')
    expect(Number(settledTask.getAttribute('opacity'))).toBeLessThanOrEqual(0.14)
    expect(workflow.getAttribute('data-live-graph-node-work-state')).toBe('none')
    expect(workflow.hasAttribute('data-live-graph-settled-context')).toBe(false)
  })

  it('renders every connected node from every session cluster at one hop when no node is selected', async () => {
    render(
      <LiveGraphCanvas graph={twoSessionGraph()} initialState={{ ...DEFAULT_LIVE_GRAPH_VIEW_STATE, focusDepth: 1 }} />
    )

    expect(await screen.findByRole('button', { name: /Session: Session A/ })).toBeTruthy()
    expect(await screen.findByRole('button', { name: /Session: Session B/ })).toBeTruthy()
    expect(await screen.findByRole('button', { name: /Workflow: Workflow A/ })).toBeTruthy()
    expect(await screen.findByRole('button', { name: /Workflow: Workflow B/ })).toBeTruthy()
    expect(await screen.findByRole('button', { name: /Task: Task A/ })).toBeTruthy()
    expect(await screen.findByRole('button', { name: /Task: Task B/ })).toBeTruthy()
  })

  it('filters node kinds and toggles the resulting orphan nodes', async () => {
    render(<LiveGraphCanvas graph={graph} initialState={{ ...DEFAULT_LIVE_GRAPH_VIEW_STATE, focusDepth: 1 }} />)

    fireEvent.click(screen.getByRole('button', { name: 'Graph settings' }))
    const settings = await screen.findByRole('dialog', { name: 'Graph settings' })

    fireEvent.click(within(settings).getByRole('switch', { name: 'Task' }))

    await waitFor(() => expect(screen.queryByRole('button', { name: /Task: Verify live app/ })).toBeNull())
    expect(screen.queryByRole('button', { name: /Agent: elephant/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /Artifact: Verification report/ })).toBeNull()
    expect(await screen.findByRole('button', { name: /Workflow: Correct artifact/ })).toBeTruthy()

    fireEvent.click(within(settings).getByRole('switch', { name: 'Orphans' }))

    expect(await screen.findByRole('button', { name: /Agent: elephant/ })).toBeTruthy()
    expect(await screen.findByRole('button', { name: /Artifact: Verification report/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Task: Verify live app/ })).toBeNull()
  })

  it('preserves earlier filter changes across successive settings updates in strict mode', async () => {
    const onStateChange = vi.fn()

    render(
      <StrictMode>
        <LiveGraphCanvas
          graph={graph}
          initialState={{ ...DEFAULT_LIVE_GRAPH_VIEW_STATE, focusDepth: 1 }}
          onStateChange={onStateChange}
        />
      </StrictMode>
    )

    fireEvent.click(screen.getByRole('button', { name: 'Graph settings' }))
    const settings = await screen.findByRole('dialog', { name: 'Graph settings' })
    const taskFilter = within(settings).getByRole('switch', { name: 'Task' })
    const orphanFilter = within(settings).getByRole('switch', { name: 'Orphans' })

    fireEvent.click(taskFilter)
    await waitFor(() => expect(taskFilter.getAttribute('data-state')).toBe('unchecked'))

    fireEvent.click(orphanFilter)
    await waitFor(() => expect(orphanFilter.getAttribute('data-state')).toBe('checked'))

    expect(taskFilter.getAttribute('data-state')).toBe('unchecked')
    expect(onStateChange).toHaveBeenCalledTimes(2)
    expect(onStateChange.mock.calls[1]?.[0]).toMatchObject({
      enabledKinds: ['session', 'project', 'workflow', 'agent', 'artifact'],
      orphans: true
    })
  })

  it('clears a selected node when its type is filtered out', async () => {
    render(<LiveGraphCanvas graph={graph} initialState={{ ...DEFAULT_LIVE_GRAPH_VIEW_STATE, focusDepth: 'all' }} />)

    fireEvent.click(await screen.findByRole('button', { name: /Task: Verify live app/ }))
    expect(await screen.findByRole('button', { name: 'Close' })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Graph settings' }))
    const settings = await screen.findByRole('dialog', { name: 'Graph settings' })
    fireEvent.click(within(settings).getByRole('switch', { name: 'Task' }))

    await waitFor(() => expect(screen.queryByRole('button', { name: 'Close' })).toBeNull())
    expect(screen.queryByRole('button', { name: /Task: Verify live app/ })).toBeNull()
  })

  it('uses the selected node and focus-depth control as the only persistent focus boundary', async () => {
    render(<LiveGraphCanvas graph={graph} initialState={{ ...DEFAULT_LIVE_GRAPH_VIEW_STATE, focusDepth: 'all' }} />)

    fireEvent.click(await screen.findByRole('button', { name: /Workflow: Correct artifact/ }))

    expect((await screen.findByRole('button', { name: /Agent: elephant/ })).getAttribute('opacity')).toBe('1')

    fireEvent.click(screen.getByRole('button', { name: 'Graph settings' }))
    const settings = await screen.findByRole('dialog', { name: 'Graph settings' })

    fireEvent.click(within(settings).getByRole('button', { name: '1 hop' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: /Agent: elephant/ })).toBeNull())

    fireEvent.click(within(settings).getByRole('button', { name: '2 hops' }))
    expect((await screen.findByRole('button', { name: /Agent: elephant/ })).getAttribute('opacity')).toBe('1')
  })

  it('clears focus when the selected node disappears', async () => {
    const { rerender } = render(
      <LiveGraphCanvas
        graph={graph}
        initialState={{ ...DEFAULT_LIVE_GRAPH_VIEW_STATE, focusDepth: 1, orphans: true }}
      />
    )

    fireEvent.click(await screen.findByRole('button', { name: /Workflow: Correct artifact/ }))

    rerender(
      <LiveGraphCanvas
        graph={{
          edges: [],
          nodes: graph.nodes.filter(node => node.kind === 'session'),
          rootId: 'session:default:root'
        }}
        initialState={{ ...DEFAULT_LIVE_GRAPH_VIEW_STATE, focusDepth: 1, orphans: true }}
      />
    )

    expect(await screen.findByRole('button', { name: /Session: Agent-ready codebase/ })).toBeTruthy()
  })

  it('closes portaled graph settings when its keep-alive surface is hidden', async () => {
    const graphView = <LiveGraphCanvas graph={graph} />
    const { rerender } = render(<div>{graphView}</div>)

    fireEvent.click(screen.getByRole('button', { name: 'Graph settings' }))
    expect(await screen.findByRole('dialog', { name: 'Graph settings' })).toBeTruthy()

    rerender(<div aria-hidden="true">{graphView}</div>)

    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Graph settings' })).toBeNull())
  })

  it('auto-fits only once so live topology updates preserve the user camera', async () => {
    const { container, rerender } = render(
      <LiveGraphCanvas graph={graph} initialState={{ ...DEFAULT_LIVE_GRAPH_VIEW_STATE, focusDepth: 'all' }} />
    )

    const svg = await screen.findByRole('application', { name: 'Graph View' })
    const world = container.querySelector('[data-live-graph-world]')

    await waitFor(() => expect(world?.getAttribute('transform')).not.toContain('scale(1)'))
    fireEvent.wheel(svg, { clientX: 450, clientY: 300, deltaY: -100 })
    const cameraAfterZoom = world?.getAttribute('transform')

    rerender(
      <LiveGraphCanvas
        graph={{
          ...graph,
          edges: [
            ...graph.edges,
            {
              id: 'edge:task-extra',
              kind: 'produced',
              sourceId: 'task:default:board:task',
              targetId: 'artifact:default:extra'
            }
          ],
          nodes: [
            ...graph.nodes,
            { entityId: 'extra', id: 'artifact:default:extra', kind: 'artifact', label: 'Extra artifact' }
          ]
        }}
        initialState={{ ...DEFAULT_LIVE_GRAPH_VIEW_STATE, focusDepth: 'all' }}
      />
    )

    await waitFor(() => expect(world?.getAttribute('transform')).toBe(cameraAfterZoom))
  })

  it.each([
    { kind: 'ordinary', orphans: false, snapshot: graph },
    { kind: 'dense', orphans: true, snapshot: denseReadabilityGraph() }
  ])('zooms the $kind graph below 20% while preserving the pointer anchor', async ({ kind, orphans, snapshot }) => {
    const { container } = render(
      <LiveGraphCanvas
        autoFit={false}
        graph={snapshot}
        initialState={{
          ...DEFAULT_LIVE_GRAPH_VIEW_STATE,
          camera: { scale: 0.2, x: 0, y: 0 },
          focusDepth: 'all',
          orphans
        }}
      />
    )

    const svg = await screen.findByRole('application', { name: 'Graph View' })
    const world = container.querySelector('[data-live-graph-world]')!
    const before = graphCamera(world)
    const anchorX = (450 - before.x) / before.scale
    const anchorY = (300 - before.y) / before.scale

    fireEvent.wheel(svg, { clientX: 450, clientY: 300, deltaY: 1_000 })

    const zoomedOut = graphCamera(world)

    expect(zoomedOut.scale).toBeLessThan(0.2)
    expect((450 - zoomedOut.x) / zoomedOut.scale).toBeCloseTo(anchorX, 8)
    expect((300 - zoomedOut.y) / zoomedOut.scale).toBeCloseTo(anchorY, 8)

    fireEvent.wheel(svg, { clientX: 450, clientY: 300, deltaY: 10_000 })

    const minimum = graphCamera(world)

    expect(minimum.scale).toBe(LIVE_GRAPH_MIN_SCALE)
    expect((450 - minimum.x) / minimum.scale).toBeCloseTo(anchorX, 8)
    expect((300 - minimum.y) / minimum.scale).toBeCloseTo(anchorY, 8)

    const glyph = container.querySelector('[data-live-graph-node-glyph]')

    expect(glyph?.querySelector('[data-live-graph-node-body]')).not.toBeNull()
    expect(glyph?.querySelector('[data-live-graph-node-kind-icon]')).not.toBeNull()
    expect(glyph?.querySelector('[data-live-graph-node-status-ring]')).not.toBeNull()
    expect(glyph?.querySelector('[data-live-graph-label]')).toBeNull()

    const hoverTarget = container.querySelector('[data-live-graph-node]')!
    const hoverLabel = container.querySelector('[data-live-graph-hover-label]')!

    fireEvent.pointerEnter(hoverTarget, { pointerId: 9 })
    fireEvent.pointerMove(hoverTarget, { pointerId: 9 })

    expect(hoverLabel.getAttribute('opacity')).toBe('1')
    expect(Number(hoverLabel.getAttribute('font-size')) * minimum.scale).toBeCloseTo(11, 6)
    expect(Number(hoverLabel.getAttribute('stroke-width')) * minimum.scale).toBeCloseTo(3, 6)
  })

  it('is still at rest and toggles labels through the shared control', async () => {
    const { container } = render(<LiveGraphCanvas graph={graph} />)

    await waitFor(() => expect(container.querySelectorAll('[data-live-graph-label]').length).toBeGreaterThan(0))
    expect(container.querySelector('[data-live-graph-pulse]')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Graph settings' }))
    const settings = await screen.findByRole('dialog', { name: 'Graph settings' })
    fireEvent.click(within(settings).getByRole('switch', { name: 'Labels' }))
    expect(container.querySelectorAll('[data-live-graph-label]')).toHaveLength(0)
  })

  it('keeps a dense overview quiet and reveals an omitted label on hover', async () => {
    const denseGraph = denseReadabilityGraph()

    const { container } = render(
      <LiveGraphCanvas
        autoFit={false}
        graph={denseGraph}
        initialState={{
          ...DEFAULT_LIVE_GRAPH_VIEW_STATE,
          camera: { scale: 0.2, x: 450, y: 300 },
          focusDepth: 'all',
          orphans: true
        }}
      />
    )

    const svg = await screen.findByRole('application', { name: 'Graph View' })
    const world = container.querySelector('[data-live-graph-world]')!

    await waitFor(() => {
      expect(world.getAttribute('transform')).not.toContain('scale(1)')
      expect(world.getAttribute('data-live-graph-lod')).toBe('overview')
    })

    const visibleLabels = () =>
      [...container.querySelectorAll('[data-live-graph-label]')].map(label => label.textContent).sort()

    expect(visibleLabels()).toEqual(['Project', 'Session', 'Workflow A', 'Workflow B'].sort())
    const baseEdges = container.querySelector('[data-live-graph-edge-batch]')!
    const baseArrows = container.querySelector('[data-live-graph-edge-batch-arrows]')!
    const highlightedEdges = container.querySelector('[data-live-graph-edge-highlight]')!
    const highlightedArrows = container.querySelector('[data-live-graph-edge-highlight-arrows]')!
    const segmentCount = (path: Element) => path.getAttribute('d')?.match(/M/g)?.length ?? 0

    await waitFor(() => expect(segmentCount(baseEdges)).toBe(denseGraph.edges.length))
    expect(container.querySelectorAll('[data-live-graph-edge-source]')).toHaveLength(0)
    expect(baseEdges.getAttribute('opacity')).toBe('0.14')
    expect(baseEdges.getAttribute('stroke')).toBe('var(--ui-text-quaternary)')
    expect(baseEdges.getAttribute('vector-effect')).toBe('non-scaling-stroke')
    expect(baseEdges.getAttribute('pointer-events')).toBe('none')
    expect(baseEdges.getAttribute('aria-hidden')).toBe('true')
    expect(segmentCount(baseArrows)).toBeGreaterThan(0)
    expect(segmentCount(baseArrows)).toBeLessThanOrEqual(denseGraph.edges.length)
    expect(highlightedEdges.getAttribute('d')).toBe('')
    expect(highlightedArrows.getAttribute('d')).toBe('')

    const unrelated = container.querySelector('[data-live-graph-node-id="task:unrelated"]')!
    const hoverLabel = container.querySelector('[data-live-graph-hover-label]')!
    const basePathBeforeHover = baseEdges.getAttribute('d')

    fireEvent.pointerEnter(unrelated, { pointerId: 7 })
    fireEvent.pointerMove(unrelated, { pointerId: 7 })
    expect(hoverLabel.textContent).toBe('Unrelated task')
    expect(hoverLabel.getAttribute('opacity')).toBe('1')
    expect(segmentCount(highlightedEdges)).toBe(1)
    expect(segmentCount(highlightedArrows)).toBe(1)
    expect(highlightedEdges.getAttribute('opacity')).toBe('1')
    expect(highlightedEdges.getAttribute('stroke')).toBe('var(--ui-text-secondary)')
    expect(highlightedEdges.getAttribute('stroke-width')).toBe('1.8')
    expect(baseEdges.getAttribute('d')).toBe(basePathBeforeHover)

    fireEvent.pointerLeave(unrelated, { pointerId: 7 })
    fireEvent.pointerMove(svg, { pointerId: 7 })
    expect(hoverLabel.getAttribute('opacity')).toBe('0')
    expect(highlightedEdges.getAttribute('d')).toBe('')
    expect(highlightedArrows.getAttribute('d')).toBe('')

    const selected = container.querySelector('[data-live-graph-node-id="task:selected"]')!
    fireEvent.click(selected)

    await waitFor(() => expect(container.querySelectorAll('[data-live-graph-label]')).toHaveLength(0))
    fireEvent.pointerMove(selected, { pointerId: 8 })
    expect(hoverLabel.textContent).toContain('Selected task')
    expect(hoverLabel.getAttribute('opacity')).toBe('1')
    await waitFor(() => expect(segmentCount(highlightedEdges)).toBe(2))
    expect(baseEdges.getAttribute('opacity')).toBe('0.025')
    expect(segmentCount(highlightedArrows)).toBeGreaterThan(0)
    expect(segmentCount(highlightedArrows)).toBeLessThanOrEqual(2)
    expect(unrelated.getAttribute('opacity')).toBe('0.2')

    fireEvent.pointerEnter(unrelated, { pointerId: 8 })
    expect(segmentCount(highlightedEdges)).toBe(3)
    fireEvent.pointerLeave(unrelated, { pointerId: 8 })
    expect(segmentCount(highlightedEdges)).toBe(2)
  }, 10_000)

  it('keeps thousands of dense connections in a constant-size edge DOM', async () => {
    const graph = denseManyEdgeGraph()

    const { container } = render(
      <LiveGraphCanvas
        graph={graph}
        initialState={{ ...DEFAULT_LIVE_GRAPH_VIEW_STATE, focusDepth: 'all', orphans: true }}
      />
    )

    const baseEdges = container.querySelector('[data-live-graph-edge-batch]')!
    const segmentCount = () => baseEdges.getAttribute('d')?.match(/M/g)?.length ?? 0

    await waitFor(() => expect(segmentCount()).toBe(graph.edges.length))
    expect(graph.edges.length).toBeGreaterThan(3_800)
    expect(container.querySelectorAll('[data-live-graph-edge-source]')).toHaveLength(0)
    expect(container.querySelectorAll('[data-live-graph-edge-batch], [data-live-graph-edge-highlight]')).toHaveLength(2)
    const baseArrows = container.querySelector('[data-live-graph-edge-batch-arrows]')!
    const arrowCount = baseArrows.getAttribute('d')?.match(/M/g)?.length ?? 0

    expect(arrowCount).toBeGreaterThan(0)
    expect(arrowCount).toBeLessThanOrEqual(graph.edges.length)
  })

  it('previews text fading without rebuilding the dense graph and persists after scrubbing', async () => {
    const onStateChange = vi.fn()

    const { container } = render(
      <LiveGraphCanvas
        autoFit={false}
        graph={denseReadabilityGraph()}
        initialState={{
          ...DEFAULT_LIVE_GRAPH_VIEW_STATE,
          camera: { scale: 0.5, x: 0, y: 0 },
          focusDepth: 'all',
          orphans: true
        }}
        onStateChange={onStateChange}
      />
    )

    const canvas = container.querySelector('[data-live-graph-canvas]') as HTMLElement
    const labelsBefore = container.querySelectorAll('[data-live-graph-label]').length
    const opacityBefore = canvas.style.getPropertyValue('--live-graph-label-opacity')

    fireEvent.click(screen.getByRole('button', { name: 'Graph settings' }))
    const settings = await screen.findByRole('dialog', { name: 'Graph settings' })
    const threshold = within(settings).getByRole('slider', { name: 'Text fade threshold' })

    fireEvent.input(threshold, { target: { value: '0' } })

    expect(canvas.style.getPropertyValue('--live-graph-label-opacity')).not.toBe(opacityBefore)
    expect(container.querySelectorAll('[data-live-graph-label]')).toHaveLength(labelsBefore)
    expect(onStateChange).not.toHaveBeenCalled()

    fireEvent.pointerUp(threshold)
    expect(onStateChange).toHaveBeenCalledTimes(1)
    expect(onStateChange.mock.calls[0]?.[0]).toMatchObject({ textFadeThreshold: 0 })
  })

  it('previews node size cheaply and rebuilds physical radii when the slider is released', async () => {
    const onStateChange = vi.fn()

    const { container } = render(
      <LiveGraphCanvas
        autoFit={false}
        graph={graph}
        initialState={{ ...DEFAULT_LIVE_GRAPH_VIEW_STATE, focusDepth: 'all' }}
        onStateChange={onStateChange}
      />
    )

    const task = await screen.findByRole('button', { name: /Task: Verify live app/ })
    const body = task.querySelector('[data-live-graph-node-body]')!
    const radiusBefore = Number(body.getAttribute('r'))
    const world = container.querySelector('[data-live-graph-world]') as SVGGElement

    fireEvent.click(screen.getByRole('button', { name: 'Graph settings' }))
    const settings = await screen.findByRole('dialog', { name: 'Graph settings' })
    const nodeSize = within(settings).getByRole('slider', { name: 'Node size' })

    fireEvent.input(nodeSize, { target: { value: '150' } })

    expect(world.style.getPropertyValue('--live-graph-node-size-preview-scale')).toBe('1.5')
    expect(within(settings).getByText('150%')).toBeTruthy()
    expect(Number(body.getAttribute('r'))).toBe(radiusBefore)
    expect(onStateChange).not.toHaveBeenCalled()

    fireEvent.pointerUp(nodeSize)

    expect(onStateChange).toHaveBeenCalledTimes(1)
    expect(onStateChange.mock.calls[0]?.[0]).toMatchObject({ nodeSize: 150 })
    expect(world.style.getPropertyValue('--live-graph-node-size-preview-scale')).toBe('1')
    expect(world.getAttribute('data-live-graph-node-size-scale')).toBe('1.5')
    expect(Number(body.getAttribute('r'))).toBeCloseTo(radiusBefore * 1.5, 8)
  })

  it('renders descendant hubs larger than leaves regardless of visible degree', async () => {
    render(<LiveGraphCanvas graph={graph} initialState={{ ...DEFAULT_LIVE_GRAPH_VIEW_STATE, focusDepth: 'all' }} />)

    const task = await screen.findByRole('button', { name: /Task: Verify live app/ })
    const artifact = await screen.findByRole('button', { name: /Artifact: Verification report/ })
    const taskBody = task.querySelector('[data-live-graph-node-body]')
    const artifactBody = artifact.querySelector('[data-live-graph-node-body]')

    expect(task.getAttribute('data-live-graph-node-degree')).toBe('3')
    expect(task.getAttribute('data-live-graph-node-reach')).toBe('2')
    expect(artifact.getAttribute('data-live-graph-node-degree')).toBe('1')
    expect(artifact.getAttribute('data-live-graph-node-reach')).toBe('0')
    expect(Number(taskBody?.getAttribute('r'))).toBeGreaterThan(Number(artifactBody?.getAttribute('r')))
  })

  it('renders a workflow hub substantially larger than its task leaves', async () => {
    render(
      <LiveGraphCanvas
        graph={forceStarGraph()}
        initialState={{ ...DEFAULT_LIVE_GRAPH_VIEW_STATE, focusDepth: 'all' }}
      />
    )

    const workflow = await screen.findByRole('button', { name: /Workflow: Force hub/ })
    const task = await screen.findByRole('button', { name: /Task: Force task 0/ })
    const workflowRadius = Number(workflow.querySelector('[data-live-graph-node-body]')?.getAttribute('r'))
    const taskRadius = Number(task.querySelector('[data-live-graph-node-body]')?.getAttribute('r'))

    expect(workflow.getAttribute('data-live-graph-node-degree')).toBe('8')
    expect(workflow.getAttribute('data-live-graph-node-reach')).toBe('8')
    expect(task.getAttribute('data-live-graph-node-degree')).toBe('1')
    expect(task.getAttribute('data-live-graph-node-reach')).toBe('0')
    expect(workflowRadius / taskRadius).toBeGreaterThan(1.5)
  })

  it('renders only semantic pulses and allows the same status to recur in a later batch', async () => {
    const pulse: LiveGraphPulse = {
      edgeId: 'edge:workflow-task',
      id: 'pulse:delegated:task',
      kind: 'delegated',
      sourceId: 'workflow:default:board:wf',
      targetId: 'task:default:board:task'
    }

    const { container, rerender } = render(<LiveGraphCanvas graph={graph} pulses={[]} />)

    expect(container.querySelector('[data-live-graph-pulse]')).toBeNull()
    rerender(<LiveGraphCanvas graph={graph} pulses={[pulse]} />)

    await waitFor(() => expect(container.querySelectorAll('[data-live-graph-pulse]').length).toBe(1))

    rerender(<LiveGraphCanvas graph={graph} pulses={[pulse]} />)
    await waitFor(() => expect(container.querySelectorAll('[data-live-graph-pulse]').length).toBe(2))
  })

  it('opens tasks with board and workflow context only when navigation is connected', async () => {
    const onOpenTask = vi.fn()

    render(<LiveGraphCanvas graph={graph} onOpenTask={onOpenTask} />)
    fireEvent.doubleClick(await screen.findByRole('button', { name: /Verify live app/ }))

    expect(onOpenTask).toHaveBeenCalledWith({ board: 'board', taskId: 'task', workflowId: 'wf' })
  })

  it('fills workflow selection from the full snapshot and reveals a task selected from its inbox', async () => {
    const nowSeconds = Math.floor(Date.now() / 1000)

    const workflowInboxGraph: LiveGraphSnapshot = {
      edges: [
        ...graph.edges,
        {
          id: 'edge:workflow-completed-task',
          kind: 'contains',
          sourceId: 'workflow:default:board:wf',
          targetId: 'task:default:board:completed'
        }
      ],
      nodes: [
        ...graph.nodes.map(node =>
          node.id === 'task:default:board:task'
            ? {
                ...node,
                createdAt: nowSeconds - 3 * 60 * 60,
                startedAt: nowSeconds - 2 * 60 * 60
              }
            : node
        ),
        {
          board: 'board',
          completedAt: nowSeconds - 5 * 60 * 60,
          createdAt: nowSeconds - 8 * 60 * 60,
          entityId: 'completed',
          id: 'task:default:board:completed',
          kind: 'task',
          label: 'Package the release',
          status: 'completed',
          workflowId: 'wf'
        },
        {
          board: 'other-board',
          createdAt: 3,
          entityId: 'task',
          id: 'task:default:other-board:task',
          kind: 'task',
          label: 'Same ids on another board',
          status: 'running',
          workflowId: 'wf'
        }
      ]
    }

    render(
      <LiveGraphCanvas
        graph={workflowInboxGraph}
        initialState={{ ...DEFAULT_LIVE_GRAPH_VIEW_STATE, enabledKinds: ['session', 'workflow'] }}
      />
    )

    fireEvent.click(await screen.findByRole('button', { name: /Workflow: Correct artifact/ }))

    const inbox = await screen.findByTestId('live-graph-workflow-inbox')
    expect(within(inbox).getByRole('button', { name: 'View task: Verify live app' })).toBeTruthy()
    expect(within(inbox).getByRole('button', { name: 'View task: Package the release' })).toBeTruthy()
    expect(within(inbox).queryByText('Same ids on another board')).toBeNull()
    expect(inbox.querySelectorAll('[data-live-graph-task-card]')).toHaveLength(1)
    expect(inbox.querySelectorAll('[data-live-graph-completed-task]')).toHaveLength(1)
    expect(within(inbox).queryByText('P2')).toBeNull()
    expect(
      Array.from(inbox.querySelectorAll('[data-live-graph-task-age]')).map(element => element.textContent)
    ).toEqual(['2h ago', '5h ago'])
    expect(inbox.querySelector('[data-live-graph-task-age] .codicon')).toBeNull()

    fireEvent.click(within(inbox).getByRole('button', { name: 'View task: Verify live app' }))

    const inspector = await screen.findByTestId('live-graph-selection-inspector')
    expect(within(inspector).getByText('Desktop interaction is ready for review.')).toBeTruthy()
    expect(await screen.findByRole('button', { name: /Task: Verify live app/ })).toBeTruthy()
  })

  it('shows full task details on a single click', async () => {
    const onOpenTask = vi.fn()

    const { container } = render(<LiveGraphCanvas graph={graph} onOpenTask={onOpenTask} />)
    expect(screen.queryByTestId('live-graph-selection-inspector')).toBeNull()
    expect(container.querySelectorAll('[data-live-graph-label]').length).toBeGreaterThan(0)

    const task = await screen.findByRole('button', { name: /Verify live app/ })
    fireEvent.click(task)

    const inspector = await screen.findByTestId('live-graph-selection-inspector')
    const details = within(inspector)
    const canvas = container.querySelector('[data-live-graph-canvas]')
    const workspace = container.querySelector('[data-live-graph-workspace]')
    const inspectorDetails = inspector.querySelector('[data-live-graph-inspector-details]')
    const hoverLabel = container.querySelector('[data-live-graph-hover-label]')!

    expect(canvas?.contains(inspector)).toBe(false)
    expect(canvas?.parentElement).toBe(workspace)
    expect(inspector.parentElement).toBe(workspace)
    expect(inspectorDetails?.parentElement).toBe(inspector)
    expect(inspector.className).toContain('overflow-x-hidden')
    expect(inspector.className).toContain('[overflow-wrap:anywhere]')
    expect(canvas?.contains(screen.getByRole('button', { name: 'Graph settings' }))).toBe(true)

    expect(details.getByText('Exercise the installed app and capture the selected task state.')).toBeTruthy()
    expect(details.getByText('Desktop interaction is ready for review.')).toBeTruthy()
    expect(details.getByText('The live verification passed.')).toBeTruthy()
    expect(details.getByText('reviewer-qa')).toBeTruthy()
    expect(details.getByText('P2')).toBeTruthy()
    expect(details.getByText('board')).toBeTruthy()
    expect(details.getByText('wf')).toBeTruthy()
    expect(details.getByText('task')).toBeTruthy()
    expect(container.querySelectorAll('[data-live-graph-label]')).toHaveLength(0)

    fireEvent.pointerMove(task, { pointerId: 11 })
    expect(hoverLabel.textContent).toContain('Verify live app')
    expect(hoverLabel.getAttribute('opacity')).toBe('1')

    fireEvent.click(details.getByRole('button', { name: 'Open task' }))
    expect(onOpenTask).toHaveBeenCalledWith({ board: 'board', taskId: 'task', workflowId: 'wf' })

    fireEvent.click(details.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByTestId('live-graph-selection-inspector')).toBeNull())
    expect(container.querySelectorAll('[data-live-graph-label]').length).toBeGreaterThan(0)
  })

  it('collapses long inspector sections independently for an at-a-glance overview', async () => {
    const longDescription = 'This is a long task description with enough detail to require a compact preview. '.repeat(
      8
    )

    const longSummary = 'This is a long task summary that should stay collapsed until it is requested. '.repeat(7)
    const compactResult = 'The verification passed.'

    const longGraph = {
      ...graph,
      nodes: graph.nodes.map(node =>
        node.id === 'task:default:board:task'
          ? { ...node, detail: longDescription, result: compactResult, summary: longSummary }
          : node
      )
    }

    render(<LiveGraphCanvas graph={longGraph} />)
    fireEvent.click(await screen.findByRole('button', { name: /Verify live app/ }))

    const inspector = await screen.findByTestId('live-graph-selection-inspector')
    const details = within(inspector)
    const sectionTexts = inspector.querySelectorAll('[data-live-graph-inspector-section-text]')
    expect(sectionTexts).toHaveLength(3)

    const description = sectionTexts[0]!
    const summary = sectionTexts[1]!
    const result = sectionTexts[2]!

    expect(description.className).toContain('line-clamp-5')
    expect(description.getAttribute('data-live-graph-inspector-truncated')).toBe('true')
    expect(summary.className).toContain('line-clamp-5')
    expect(result.className).not.toContain('line-clamp-5')
    expect(details.queryByRole('button', { name: 'Show more Result' })).toBeNull()

    const expandDescription = details.getByRole('button', { name: 'Show more Description' })
    expect(expandDescription.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(expandDescription)

    expect(description.className).not.toContain('line-clamp-5')
    expect(description.getAttribute('data-live-graph-inspector-truncated')).toBeNull()
    expect(summary.className).toContain('line-clamp-5')
    expect(details.getByRole('button', { name: 'Show less Description' }).getAttribute('aria-expanded')).toBe('true')
  })

  it('shows task details when pointer capture retargets the release to the canvas', async () => {
    render(<LiveGraphCanvas graph={graph} />)

    const svg = await screen.findByRole('application', { name: 'Graph View' })
    const task = await screen.findByRole('button', { name: /Verify live app/ })

    fireEvent.pointerDown(task, { button: 0, clientX: 100, clientY: 100, pointerId: 8 })
    fireEvent.pointerUp(svg, { clientX: 100, clientY: 100, pointerId: 8 })
    fireEvent.click(svg)

    expect(await screen.findByTestId('live-graph-selection-inspector')).toBeTruthy()
    expect(task.getAttribute('aria-pressed')).toBe('true')
  })

  it('attaches a Shift-clicked node without selecting it, then preserves normal click selection', async () => {
    const onAttachNode = vi.fn()

    render(<LiveGraphCanvas graph={graph} onAttachNode={onAttachNode} />)

    const svg = await screen.findByRole('application', { name: 'Graph View' })
    const task = await screen.findByRole('button', { name: /Verify live app/ })

    fireEvent.pointerDown(task, {
      button: 0,
      clientX: 100,
      clientY: 100,
      pointerId: 18,
      shiftKey: true
    })
    fireEvent.pointerUp(svg, { clientX: 100, clientY: 100, pointerId: 18, shiftKey: true })
    fireEvent.click(task, { shiftKey: true })

    expect(onAttachNode).toHaveBeenCalledTimes(1)
    expect(onAttachNode).toHaveBeenCalledWith(graph.nodes.find(node => node.id === 'task:default:board:task'))
    expect(screen.queryByTestId('live-graph-selection-inspector')).toBeNull()
    expect(task.getAttribute('aria-pressed')).toBe('false')

    const agent = await screen.findByRole('button', { name: /Agent: elephant/ })
    fireEvent.click(agent)

    expect(await screen.findByTestId('live-graph-selection-inspector')).toBeTruthy()
    expect(agent.getAttribute('aria-pressed')).toBe('true')
  })

  it('does not attach a node when a Shift press becomes a drag', async () => {
    const onAttachNode = vi.fn()

    render(<LiveGraphCanvas graph={graph} onAttachNode={onAttachNode} />)

    const svg = await screen.findByRole('application', { name: 'Graph View' })
    const task = await screen.findByRole('button', { name: /Verify live app/ })

    fireEvent.pointerDown(task, {
      button: 0,
      clientX: 100,
      clientY: 100,
      pointerId: 19,
      shiftKey: true
    })
    fireEvent.pointerMove(svg, { clientX: 140, clientY: 140, pointerId: 19, shiftKey: true })
    fireEvent.pointerUp(svg, { clientX: 140, clientY: 140, pointerId: 19, shiftKey: true })
    fireEvent.click(task, { shiftKey: true })

    expect(onAttachNode).not.toHaveBeenCalled()
    expect(screen.queryByTestId('live-graph-selection-inspector')).toBeNull()
  })

  it('supports Shift+Enter as the keyboard equivalent for attaching a node', async () => {
    const onAttachNode = vi.fn()

    render(<LiveGraphCanvas graph={graph} onAttachNode={onAttachNode} />)

    const task = await screen.findByRole('button', { name: /Verify live app/ })
    fireEvent.keyDown(task, { key: 'Enter', shiftKey: true })

    expect(onAttachNode).toHaveBeenCalledWith(graph.nodes.find(node => node.id === 'task:default:board:task'))
    expect(screen.queryByTestId('live-graph-selection-inspector')).toBeNull()
  })

  it('closes the inspector on an empty-canvas click but keeps it open while panning', async () => {
    render(<LiveGraphCanvas graph={graph} />)

    const svg = await screen.findByRole('application', { name: 'Graph View' })
    const task = await screen.findByRole('button', { name: /Verify live app/ })

    fireEvent.click(task)
    expect(await screen.findByTestId('live-graph-selection-inspector')).toBeTruthy()

    fireEvent.pointerDown(svg, { button: 0, clientX: 20, clientY: 20, pointerId: 9 })
    fireEvent.pointerMove(svg, { clientX: 80, clientY: 60, pointerId: 9 })
    fireEvent.pointerUp(svg, { clientX: 80, clientY: 60, pointerId: 9 })
    fireEvent.click(svg)

    expect(screen.getByTestId('live-graph-selection-inspector')).toBeTruthy()
    expect(task.getAttribute('aria-pressed')).toBe('true')

    fireEvent.click(svg)

    await waitFor(() => expect(screen.queryByTestId('live-graph-selection-inspector')).toBeNull())
    expect(task.getAttribute('aria-pressed')).toBe('false')
  })

  it('drags a node, updates its links, and keeps the camera fixed', async () => {
    const { container } = render(
      <LiveGraphCanvas
        autoFit={false}
        graph={graph}
        initialState={{ ...DEFAULT_LIVE_GRAPH_VIEW_STATE, focusDepth: 'all' }}
      />
    )

    const svg = await screen.findByRole('application', { name: 'Graph View' })
    const agent = await screen.findByRole('button', { name: /Agent: elephant/ })
    const world = container.querySelector('[data-live-graph-world]')

    const link = container.querySelector(
      '[data-live-graph-edge-source="task:default:board:task"][data-live-graph-edge-target="agent:default:board:task:worker"]'
    )

    const agentBefore = graphNodePosition(agent)
    const worldBefore = world?.getAttribute('transform')

    const linkBefore = [
      link?.getAttribute('x1'),
      link?.getAttribute('y1'),
      link?.getAttribute('x2'),
      link?.getAttribute('y2')
    ]

    fireEvent.pointerDown(agent, { button: 0, clientX: 100, clientY: 100, pointerId: 7 })
    fireEvent.pointerMove(svg, { clientX: 180, clientY: 140, pointerId: 7 })

    await waitFor(() => {
      const agentAfter = graphNodePosition(agent)

      expect(agentAfter.x - agentBefore.x).toBeCloseTo(80, 0)
      expect(agentAfter.y - agentBefore.y).toBeCloseTo(40, 0)
    })

    expect(world?.getAttribute('transform')).toBe(worldBefore)
    expect([
      link?.getAttribute('x1'),
      link?.getAttribute('y1'),
      link?.getAttribute('x2'),
      link?.getAttribute('y2')
    ]).not.toEqual(linkBefore)

    fireEvent.pointerUp(svg, { clientX: 180, clientY: 140, pointerId: 7 })
    fireEvent.click(agent)
    expect(agent.getAttribute('aria-pressed')).toBe('false')
  })
  it('replays the graph outward from its root', async () => {
    const { container } = render(
      <LiveGraphCanvas
        autoFit={false}
        graph={graph}
        initialState={{ ...DEFAULT_LIVE_GRAPH_VIEW_STATE, focusDepth: 'all' }}
      />
    )

    const root = await screen.findByRole('button', { name: /Session: Agent-ready codebase/ })
    const agent = await screen.findByRole('button', { name: /Agent: elephant/ })
    const task = await screen.findByRole('button', { name: /Task: Verify live app/ })

    fireEvent.click(task)
    fireEvent.click(screen.getByRole('button', { name: 'Graph settings' }))
    const settings = await screen.findByRole('dialog', { name: 'Graph settings' })
    fireEvent.click(within(settings).getByRole('button', { name: 'Animate' }))

    expect(Number(root.getAttribute('opacity'))).toBe(1)
    expect(Number(task.getAttribute('opacity'))).toBe(0)
    expect(Number(agent.getAttribute('opacity'))).toBe(0)
    expect(
      container.querySelector('[data-live-graph-edge-source="session:default:root"]')?.getAttribute('opacity')
    ).toBe('0')

    await waitFor(() => expect(Number(agent.getAttribute('opacity'))).toBeGreaterThan(0), { timeout: 1600 })
  })

  it('restores the settled graph when its pane is hidden during replay', async () => {
    const graphView = (
      <LiveGraphCanvas
        autoFit={false}
        graph={graph}
        initialState={{ ...DEFAULT_LIVE_GRAPH_VIEW_STATE, focusDepth: 'all' }}
      />
    )

    const { container, rerender } = render(<div>{graphView}</div>)
    const agent = await screen.findByRole('button', { name: /Agent: elephant/ })
    const settledPosition = graphNodePosition(agent)

    fireEvent.click(screen.getByRole('button', { name: 'Graph settings' }))
    const settings = await screen.findByRole('dialog', { name: 'Graph settings' })
    fireEvent.click(within(settings).getByRole('button', { name: 'Animate' }))
    expect(Number(agent.getAttribute('opacity'))).toBe(0)

    rerender(<div aria-hidden="true">{graphView}</div>)

    const hiddenAgent = container.querySelector('[data-live-graph-node-kind="agent"]')

    await waitFor(() => expect(hiddenAgent?.getAttribute('opacity')).toBe('1'))
    const restoredPosition = graphNodePosition(hiddenAgent!)

    expect(Math.hypot(restoredPosition.x - settledPosition.x, restoredPosition.y - settledPosition.y)).toBeLessThan(
      0.001
    )
  })
})
