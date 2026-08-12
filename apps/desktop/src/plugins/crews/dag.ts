/**
 * Pure DAG helpers for the visual workflow builder — topological layering
 * (Kahn), cycle detection, and auto-layout. Kept free of React/DOM so they
 * are unit-testable and shared between the canvas and the save path.
 */
import type { WorkflowEdge, WorkflowTask } from './types'

/** Detect a cycle in a task graph (DFS three-colour). */
export function hasCycle(taskIds: string[], edges: WorkflowEdge[]): boolean {
  const adj = new Map<string, string[]>()

  for (const id of taskIds) {adj.set(id, [])}

  for (const e of edges) {
    const list = adj.get(e.from)

    if (list) {list.push(e.to)}
  }

  const WHITE = 0
  const GRAY = 1
  const BLACK = 2
  const color = new Map<string, number>()

  for (const id of taskIds) {color.set(id, WHITE)}

  const dfs = (id: string): boolean => {
    color.set(id, GRAY)

    for (const next of adj.get(id) ?? []) {
      const c = color.get(next)

      if (c === GRAY) {return true}

      if (c === WHITE && dfs(next)) {return true}
    }

    color.set(id, BLACK)

    return false
  }

  for (const id of taskIds) {
    if (color.get(id) === WHITE && dfs(id)) {return true}
  }

  return false
}

/** Kahn BFS layers — tasks in the same layer can run in parallel. */
export function topoLayers(taskIds: string[], edges: WorkflowEdge[]): string[][] {
  const indegree = new Map<string, number>()
  const adj = new Map<string, string[]>()

  for (const id of taskIds) {
    indegree.set(id, 0)
    adj.set(id, [])
  }

  for (const e of edges) {
    adj.get(e.from)?.push(e.to)
    indegree.set(e.to, (indegree.get(e.to) ?? 0) + 1)
  }

  const layers: string[][] = []
  let frontier = taskIds.filter(id => (indegree.get(id) ?? 0) === 0)

  while (frontier.length > 0) {
    layers.push(frontier)
    const next: string[] = []

    for (const node of frontier) {
      for (const child of adj.get(node) ?? []) {
        indegree.set(child, (indegree.get(child) ?? 0) - 1)

        if ((indegree.get(child) ?? 0) === 0) {next.push(child)}
      }
    }

    frontier = next
  }

  return layers
}

/** True when an edge already exists (either direction is treated as a
 *  duplicate — the builder only allows one dependency per pair). */
export function edgeExists(edges: WorkflowEdge[], from: string, to: string): boolean {
  return edges.some(e => e.from === from && e.to === to)
}

/**
 * Auto-layout: Kahn layers become parallel columns, left→right, with each
 * layer's nodes vertically centred. Assigns x/y onto copies of the tasks.
 */
export function autoLayout(
  tasks: WorkflowTask[],
  edges: WorkflowEdge[],
  opts: { columnGap?: number; rowGap?: number; nodeWidth?: number; nodeHeight?: number } = {}
): WorkflowTask[] {
  const { columnGap = 220, rowGap = 120, nodeWidth = 180, nodeHeight = 72 } = opts

  const layers = topoLayers(
    tasks.map(t => t.id),
    edges
  )

  const position = new Map<string, { x: number; y: number }>()

  const layerMax = Math.max(1, ...layers.map(l => l.length))
  const totalHeight = layerMax * nodeHeight + (layerMax - 1) * rowGap

  layers.forEach((layer, layerIndex) => {
    const columnTop = totalHeight - (layer.length * nodeHeight + (layer.length - 1) * rowGap)
    layer.forEach((taskId, rowIndex) => {
      position.set(taskId, {
        x: 80 + layerIndex * (nodeWidth + columnGap),
        y: columnTop / 2 + rowIndex * (nodeHeight + rowGap)
      })
    })
  })

  return tasks.map(task => {
    const pos = position.get(task.id)

    return pos ? { ...task, x: pos.x, y: pos.y } : task
  })
}

/** Tasks that are currently blocked (have an unfinished dependency). */
export function blockedTaskIds(taskIds: string[], edges: WorkflowEdge[], status: Record<string, string>): Set<string> {
  const blocked = new Set<string>()

  for (const e of edges) {
    if (status[e.from] !== 'done') {blocked.add(e.to)}
  }

  return blocked
}
