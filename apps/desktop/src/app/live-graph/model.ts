import { loopToolLabel, loopWorkerCurrentTool } from '@/app/chat/loop-selectors'
import type { TenantLoopSource, TenantLoopTask } from '@/app/chat/loop-state'
import type { LoopagentActivity } from '@/store/loopagents'
import type { SubagentProgress } from '@/store/subagents'

export type LiveGraphNodeKind = 'agent' | 'artifact' | 'project' | 'session' | 'task' | 'workflow'
export type LiveGraphEdgeKind = 'contains' | 'delegated_to' | 'depends_on' | 'produced'

export interface LiveGraphNode {
  assignee?: string
  board?: string
  completedAt?: number
  createdAt?: number
  currentTool?: string
  detail?: string
  entityId: string
  id: string
  kind: LiveGraphNodeKind
  label: string
  openable?: boolean
  path?: string
  priority?: number
  revision?: number
  result?: string
  startedAt?: number
  status?: string
  summary?: string
  workflowId?: string
}

export interface LiveGraphEdge {
  id: string
  kind: LiveGraphEdgeKind
  sourceId: string
  targetId: string
}

export interface LiveGraphSnapshot {
  edges: LiveGraphEdge[]
  nodes: LiveGraphNode[]
  rootId?: string
}

export interface SessionLiveGraphInput {
  loopagents: readonly LoopagentActivity[]
  profile: string
  project?: {
    boardSlug?: null | string
    id: string
    name: string
  }
  session: {
    cwd: null | string
    id: string
    openable?: boolean
    title: string
  }
  sources: readonly TenantLoopSource[]
  subagents: readonly SubagentProgress[]
}

export type LiveGraphPulseKind =
  | 'activated'
  | 'blocked'
  | 'completed'
  | 'delegated'
  | 'failed'
  | 'produced'
  | 'status_changed'
  | 'task_added'

export interface LiveGraphPulse {
  edgeId: string
  id: string
  kind: LiveGraphPulseKind
  sourceId: string
  targetId: string
}

type IdPart = number | string

const clean = (value: unknown): string => (typeof value === 'string' ? value.trim() : '')
const encodeIdPart = (value: IdPart): string => encodeURIComponent(String(value))

const STATUS_ALIASES: Record<string, string> = {
  active: 'running',
  canceled: 'closed',
  cancelled: 'closed',
  claimed: 'running',
  done: 'completed',
  error: 'failed',
  in_progress: 'running',
  needs_review: 'blocked',
  review_required: 'blocked',
  skipped: 'completed',
  succeeded: 'completed',
  success: 'completed'
}

export const LIVE_GRAPH_ATTENTION_STATUSES: ReadonlySet<string> = new Set(['blocked', 'failed', 'interrupted'])
export const LIVE_GRAPH_COMPLETED_STATUSES: ReadonlySet<string> = new Set(['closed', 'completed'])
export const LIVE_GRAPH_SETTLED_STATUSES: ReadonlySet<string> = new Set([
  'closed',
  'completed',
  'failed',
  'interrupted',
  'unknown'
])
export const LIVE_GRAPH_WAITING_STATUSES: ReadonlySet<string> = new Set([
  'paused',
  'pending',
  'queued',
  'ready',
  'review',
  'scheduled',
  'todo',
  'triage'
])

export function normalizeLiveGraphStatus(value: unknown): string {
  const status = clean(value).toLowerCase().replaceAll('-', '_').replaceAll(' ', '_') || 'unknown'

  return STATUS_ALIASES[status] || status
}

const normalizedStatus = (value: unknown): string | undefined => {
  const status = clean(value)

  return status ? normalizeLiveGraphStatus(status) : undefined
}

const finiteMax = (...values: unknown[]): number | undefined => {
  const numbers = values.filter((value): value is number => typeof value === 'number' && Number.isFinite(value))

  return numbers.length ? Math.max(...numbers) : undefined
}

export function liveGraphNodeId(kind: LiveGraphNodeKind, ...parts: IdPart[]): string {
  return [kind, ...parts.map(encodeIdPart)].join(':')
}

export function liveGraphEdgeId(kind: LiveGraphEdgeKind, sourceId: string, targetId: string): string {
  return `edge:${kind}:${encodeIdPart(sourceId)}:${encodeIdPart(targetId)}`
}

const boardName = (source: TenantLoopSource): string => clean(source.board) || 'default'

const sourceRevision = (source: TenantLoopSource): number | undefined =>
  finiteMax(source.source_revision, source.latest_event_id)

const taskKey = (board: string, taskId: string): string => `${board}\u0000${taskId}`

const workflowIdsForSource = (source: TenantLoopSource): string[] =>
  Array.from(
    new Set(
      [
        ...(source.workflow_ids || []),
        ...(source.workflows || []).map(workflow => workflow.id),
        source.workflow_id,
        source.root_task_id,
        ...(source.tasks || []).map(task => task.workflow_id)
      ]
        .map(clean)
        .filter(Boolean)
    )
  ).sort()

const workflowForTask = (source: TenantLoopSource, task: TenantLoopTask, workflowIds: readonly string[]): string =>
  clean(task.workflow_id) ||
  clean(source.workflow_id) ||
  (workflowIds.length === 1 ? workflowIds[0] : '') ||
  'unassigned'

const artifactPath = (value: string): string => value.trim().replaceAll('\\', '/')
const artifactLabel = (value: string): string => value.split('/').filter(Boolean).at(-1) || value

interface TaskContext {
  assignee: string
  board: string
  nodeId: string
  revision?: number
  task: TenantLoopTask
  workflowNodeId: string
}

interface TaskLinkReference {
  board: string
  childId: string
  parentId: string
}

interface StructuralTaskEdge {
  child: TaskContext
  parent: TaskContext
}

const structuralTaskEdgeKey = (edge: StructuralTaskEdge): string => `${edge.parent.nodeId}\u0000${edge.child.nodeId}`

/** Match the Loop task canvas: keep immediate DAG edges plus malformed-cycle back edges. */
function reduceStructuralTaskEdges(edges: readonly StructuralTaskEdge[]): StructuralTaskEdge[] {
  const sorted = [...edges].sort(
    (left, right) =>
      left.parent.nodeId.localeCompare(right.parent.nodeId) || left.child.nodeId.localeCompare(right.child.nodeId)
  )

  const outgoing = new Map<string, StructuralTaskEdge[]>()
  const taskIds = new Set<string>()

  for (const edge of sorted) {
    const entries = outgoing.get(edge.parent.nodeId)

    if (entries) {
      entries.push(edge)
    } else {
      outgoing.set(edge.parent.nodeId, [edge])
    }

    taskIds.add(edge.parent.nodeId)
    taskIds.add(edge.child.nodeId)
  }

  const visiting = new Set<string>()
  const visited = new Set<string>()
  const backEdgeKeys = new Set<string>()

  const visit = (taskId: string): void => {
    visiting.add(taskId)

    for (const edge of outgoing.get(taskId) || []) {
      if (visiting.has(edge.child.nodeId)) {
        backEdgeKeys.add(structuralTaskEdgeKey(edge))
      } else if (!visited.has(edge.child.nodeId)) {
        visit(edge.child.nodeId)
      }
    }

    visiting.delete(taskId)
    visited.add(taskId)
  }

  for (const taskId of [...taskIds].sort()) {
    if (!visited.has(taskId)) {
      visit(taskId)
    }
  }

  const dagEdges = sorted.filter(edge => !backEdgeKeys.has(structuralTaskEdgeKey(edge)))
  const dagOutgoing = new Map<string, StructuralTaskEdge[]>()

  for (const edge of dagEdges) {
    const entries = dagOutgoing.get(edge.parent.nodeId)

    if (entries) {
      entries.push(edge)
    } else {
      dagOutgoing.set(edge.parent.nodeId, [edge])
    }
  }

  const hasAlternatePath = (excluded: StructuralTaskEdge): boolean => {
    const pending = (dagOutgoing.get(excluded.parent.nodeId) || [])
      .filter(edge => edge !== excluded)
      .map(edge => edge.child.nodeId)

    const seen = new Set<string>()

    while (pending.length > 0) {
      const taskId = pending.pop()!

      if (taskId === excluded.child.nodeId) {
        return true
      }

      if (seen.has(taskId)) {
        continue
      }

      seen.add(taskId)

      for (const edge of dagOutgoing.get(taskId) || []) {
        pending.push(edge.child.nodeId)
      }
    }

    return false
  }

  return sorted.filter(edge => backEdgeKeys.has(structuralTaskEdgeKey(edge)) || !hasAlternatePath(edge))
}

interface AgentCandidate {
  aliases: string[]
  board: string
  identity: string
  label: string
  priority: number
  revision?: number
  status?: string
  taskId: string
}

interface AgentRecord {
  aliases: Set<string>
  node: LiveGraphNode
  priority: number
}

const agentRunAlias = (board: string, taskId: string, runId: number): string => `run:${board}:${taskId}:${runId}`
const agentSessionAlias = (profile: string, sessionId: string): string => `session:${profile}:${sessionId}`

const agentAssigneeAlias = (board: string, taskId: string, assignee: string): string =>
  `assignee:${board}:${taskId}:${assignee}`

const agentActivityAlias = (board: string, activityId: string): string => `activity:${board}:${activityId}`
const subagentAlias = (id: string): string => `subagent:${id}`

const shouldReplaceAgentSemantics = (record: AgentRecord, candidate: AgentCandidate): boolean => {
  if (!record.node.status && candidate.status) {
    return true
  }

  if (candidate.revision !== undefined && record.node.revision !== undefined) {
    return (
      candidate.revision > record.node.revision ||
      (candidate.revision === record.node.revision && candidate.priority >= record.priority)
    )
  }

  if (candidate.revision !== undefined) {
    return true
  }

  return record.node.revision === undefined && candidate.priority >= record.priority
}

/** Build the stable semantic graph only. Animation timing and gateway events stay outside this model. */
export function buildSessionLiveGraph(input: SessionLiveGraphInput): LiveGraphSnapshot {
  const profile = clean(input.profile) || 'default'
  const sessionNodeId = liveGraphNodeId('session', profile, input.session.id)
  const nodes = new Map<string, LiveGraphNode>()
  const edges = new Map<string, LiveGraphEdge>()

  const putNode = (node: LiveGraphNode): void => {
    const previous = nodes.get(node.id)

    if (!previous) {
      nodes.set(node.id, node)

      return
    }

    const useIncoming =
      node.revision !== undefined && (previous.revision === undefined || node.revision >= previous.revision)

    nodes.set(node.id, {
      ...previous,
      ...(useIncoming ? node : {}),
      revision: finiteMax(previous.revision, node.revision)
    })
  }

  const putEdge = (kind: LiveGraphEdgeKind, sourceId: string, targetId: string): LiveGraphEdge | null => {
    if (sourceId === targetId) {
      return null
    }

    const edge: LiveGraphEdge = {
      id: liveGraphEdgeId(kind, sourceId, targetId),
      kind,
      sourceId,
      targetId
    }

    edges.set(edge.id, edge)

    return edge
  }

  putNode({
    entityId: input.session.id,
    id: sessionNodeId,
    kind: 'session',
    label: clean(input.session.title) || 'Session',
    openable: input.session.openable !== false,
    ...(clean(input.session.cwd) ? { path: clean(input.session.cwd) } : {})
  })

  if (input.project) {
    const projectNodeId = liveGraphNodeId('project', profile, input.project.id)
    putNode({
      ...(clean(input.project.boardSlug) ? { board: clean(input.project.boardSlug) } : {}),
      entityId: input.project.id,
      id: projectNodeId,
      kind: 'project',
      label: clean(input.project.name) || input.project.id
    })
    putEdge('contains', projectNodeId, sessionNodeId)
  }

  const taskContexts: TaskContext[] = []
  const taskByKey = new Map<string, TaskContext>()
  const taskLinkReferences: TaskLinkReference[] = []
  const sources = [...input.sources].sort((left, right) => boardName(left).localeCompare(boardName(right)))

  for (const source of sources) {
    const board = boardName(source)
    const revision = sourceRevision(source)
    const workflowIds = workflowIdsForSource(source)
    const workflows = new Map((source.workflows || []).map(workflow => [clean(workflow.id), workflow]))
    const workersByTask = new Map((source.workers || []).map(worker => [worker.task_id, worker]))
    const tasks = [...(source.tasks || [])].sort((left, right) => left.id.localeCompare(right.id))
    const effectiveWorkflowIds = new Set(workflowIds)

    for (const workflowId of workflows.keys()) {
      if (workflowId) {
        effectiveWorkflowIds.add(workflowId)
      }
    }

    for (const task of tasks) {
      effectiveWorkflowIds.add(workflowForTask(source, task, workflowIds))
    }

    for (const workflowId of [...effectiveWorkflowIds].sort()) {
      const workflowNodeId = liveGraphNodeId('workflow', profile, board, workflowId)
      const rootTask = tasks.find(task => task.id === workflowId)
      const workflow = workflows.get(workflowId)
      putNode({
        board,
        ...(typeof workflow?.created_at === 'number' ? { createdAt: workflow.created_at } : {}),
        entityId: workflowId,
        id: workflowNodeId,
        kind: 'workflow',
        label: clean(workflow?.title) || clean(rootTask?.title) || `Workflow ${workflowId}`,
        revision: workflow?.revision ?? revision,
        status: normalizedStatus(workflow?.status),
        workflowId
      })
      putEdge('contains', sessionNodeId, workflowNodeId)
    }

    for (const task of tasks) {
      const workflowId = workflowForTask(source, task, workflowIds)
      const workflowNodeId = liveGraphNodeId('workflow', profile, board, workflowId)
      const nodeId = liveGraphNodeId('task', profile, board, task.id)

      const currentTool = loopWorkerCurrentTool({
        latestRun: task.latest_run,
        workerActivity: task.worker_activity || workersByTask.get(task.id)
      })

      const context: TaskContext = {
        assignee: clean(task.assignee),
        board,
        nodeId,
        revision,
        task,
        workflowNodeId
      }

      taskContexts.push(context)
      taskByKey.set(taskKey(board, task.id), context)
      putNode({
        ...(clean(task.assignee) ? { assignee: clean(task.assignee) } : {}),
        board,
        ...(typeof task.completed_at === 'number' ? { completedAt: task.completed_at } : {}),
        ...(typeof task.created_at === 'number' ? { createdAt: task.created_at } : {}),
        ...(currentTool ? { currentTool } : {}),
        ...(clean(task.body) ? { detail: clean(task.body) } : {}),
        entityId: task.id,
        id: nodeId,
        kind: 'task',
        label: clean(task.title) || task.id,
        ...(typeof task.priority === 'number' && Number.isFinite(task.priority) ? { priority: task.priority } : {}),
        revision,
        ...(clean(task.result) ? { result: clean(task.result) } : {}),
        ...(typeof task.started_at === 'number' ? { startedAt: task.started_at } : {}),
        status: normalizedStatus(task.status),
        ...(clean(task.latest_summary) ? { summary: clean(task.latest_summary) } : {}),
        workflowId
      })
    }

    for (const link of source.links || []) {
      const parentId = clean(link.parent_id)
      const childId = clean(link.child_id)

      if (parentId && childId) {
        taskLinkReferences.push({ board, childId, parentId })
      }
    }
  }

  const structuralTaskEdges = new Map<string, StructuralTaskEdge>()

  const addTaskRelationship = (board: string, parentId: string, childId: string): void => {
    const parent = taskByKey.get(taskKey(board, clean(parentId)))
    const child = taskByKey.get(taskKey(board, clean(childId)))

    if (!parent || !child || parent.nodeId === child.nodeId) {
      return
    }

    if (parent.workflowNodeId !== child.workflowNodeId) {
      // A cross-workflow prerequisite is useful context, but it does not make
      // the child part of the other workflow's structural task tree.
      putEdge('depends_on', child.nodeId, parent.nodeId)

      return
    }

    structuralTaskEdges.set(`${parent.nodeId}\u0000${child.nodeId}`, { child, parent })
  }

  for (const link of taskLinkReferences) {
    addTaskRelationship(link.board, link.parentId, link.childId)
  }

  for (const context of taskContexts) {
    const parents = new Set([...(context.task.included_parent_ids || []), ...(context.task.links?.parents || [])])
    const children = new Set([...(context.task.included_child_ids || []), ...(context.task.links?.children || [])])

    for (const parentId of parents) {
      addTaskRelationship(context.board, parentId, context.task.id)
    }

    for (const childId of children) {
      addTaskRelationship(context.board, context.task.id, childId)
    }
  }

  const structuralParentsByTask = new Map<string, Set<string>>()
  const structuralNeighborsByTask = new Map<string, Set<string>>()

  const addToSet = (index: Map<string, Set<string>>, key: string, value: string): void => {
    const values = index.get(key)

    if (values) {
      values.add(value)
    } else {
      index.set(key, new Set([value]))
    }
  }

  for (const { child, parent } of reduceStructuralTaskEdges([...structuralTaskEdges.values()])) {
    putEdge('depends_on', child.nodeId, parent.nodeId)
    addToSet(structuralParentsByTask, child.nodeId, parent.nodeId)
    addToSet(structuralNeighborsByTask, parent.nodeId, child.nodeId)
    addToSet(structuralNeighborsByTask, child.nodeId, parent.nodeId)
  }

  const tasksByWorkflow = new Map<string, TaskContext[]>()

  for (const context of taskByKey.values()) {
    const workflowTasks = tasksByWorkflow.get(context.workflowNodeId)

    if (workflowTasks) {
      workflowTasks.push(context)
    } else {
      tasksByWorkflow.set(context.workflowNodeId, [context])
    }
  }

  for (const [workflowNodeId, contexts] of [...tasksByWorkflow].sort(([left], [right]) => left.localeCompare(right))) {
    const contextByNodeId = new Map(contexts.map(context => [context.nodeId, context]))
    const orderedTaskNodeIds = [...contextByNodeId.keys()].sort()
    const pending = new Set(orderedTaskNodeIds)
    let nextTaskIndex = 0

    while (pending.size > 0) {
      while (!pending.has(orderedTaskNodeIds[nextTaskIndex]!)) {
        nextTaskIndex += 1
      }

      const startId = orderedTaskNodeIds[nextTaskIndex]!
      const component = new Set<string>()
      const queue = [startId]
      let queueIndex = 0

      while (queueIndex < queue.length) {
        const taskNodeId = queue[queueIndex++]!

        if (component.has(taskNodeId) || !contextByNodeId.has(taskNodeId)) {
          continue
        }

        component.add(taskNodeId)
        pending.delete(taskNodeId)

        for (const neighborId of structuralNeighborsByTask.get(taskNodeId) || []) {
          if (!component.has(neighborId)) {
            queue.push(neighborId)
          }
        }
      }

      const roots = [...component]
        .filter(taskNodeId => (structuralParentsByTask.get(taskNodeId)?.size || 0) === 0)
        .sort()

      // Malformed cyclic task data has no natural root. Keep that component
      // reachable from its workflow with one stable, deterministic anchor.
      for (const rootTaskNodeId of roots.length > 0 ? roots : [[...component].sort()[0]!]) {
        putEdge('contains', workflowNodeId, rootTaskNodeId)
      }
    }
  }

  const agents = new Map<string, AgentRecord>()
  const agentIdByAlias = new Map<string, string>()

  const upsertAgent = (candidate: AgentCandidate): string => {
    const existingId = candidate.aliases.map(alias => agentIdByAlias.get(alias)).find(Boolean)

    const nodeId =
      existingId || liveGraphNodeId('agent', profile, candidate.board, candidate.taskId, candidate.identity)

    const existing = agents.get(nodeId)

    if (!existing) {
      const record: AgentRecord = {
        aliases: new Set(candidate.aliases),
        node: {
          board: candidate.board,
          entityId: candidate.identity,
          id: nodeId,
          kind: 'agent',
          label: candidate.label,
          revision: candidate.revision,
          status: candidate.status
        },
        priority: candidate.priority
      }

      agents.set(nodeId, record)

      for (const alias of record.aliases) {
        agentIdByAlias.set(alias, nodeId)
      }

      putNode(record.node)

      return nodeId
    }

    const replaceSemantics = shouldReplaceAgentSemantics(existing, candidate)
    existing.node = {
      ...existing.node,
      ...(replaceSemantics
        ? {
            label: candidate.label,
            status: candidate.status
          }
        : {}),
      revision: finiteMax(existing.node.revision, candidate.revision)
    }

    if (replaceSemantics) {
      existing.priority = candidate.priority
    }

    for (const alias of candidate.aliases) {
      existing.aliases.add(alias)
      agentIdByAlias.set(alias, nodeId)
    }

    putNode(existing.node)

    return nodeId
  }

  const taskBoundAgentIds = new Set<string>()

  const connectAgent = (agentNodeId: string, taskNodeId?: string): void => {
    if (taskNodeId) {
      taskBoundAgentIds.add(agentNodeId)
      edges.delete(liveGraphEdgeId('delegated_to', sessionNodeId, agentNodeId))
      putEdge('delegated_to', taskNodeId, agentNodeId)

      return
    }

    if (!taskBoundAgentIds.has(agentNodeId)) {
      putEdge('delegated_to', sessionNodeId, agentNodeId)
    }
  }

  const connectArtifact = (agentNodeId: string, rawPath: string): void => {
    const path = artifactPath(rawPath)

    if (!path) {
      return
    }

    const artifactNodeId = liveGraphNodeId('artifact', profile, input.session.id, path)
    putNode({ entityId: path, id: artifactNodeId, kind: 'artifact', label: artifactLabel(path), path })
    putEdge('produced', agentNodeId, artifactNodeId)
  }

  for (const context of taskContexts) {
    if (context.assignee) {
      const alias = agentAssigneeAlias(context.board, context.task.id, context.assignee)

      const agentNodeId = upsertAgent({
        aliases: [alias],
        board: context.board,
        identity: `assignee:${context.assignee}`,
        label: context.assignee,
        priority: 0,
        revision: context.revision,
        taskId: context.task.id
      })

      connectAgent(agentNodeId, context.nodeId)
    }
  }

  const sourceWorkerEntries = sources.flatMap(source => {
    const board = boardName(source)
    const byTask = new Map((source.tasks || []).map(task => [task.id, task]))

    const workers = [
      ...(source.workers || []),
      ...(source.tasks || []).flatMap(task => (task.worker_activity ? [task.worker_activity] : []))
    ]

    return workers.map(worker => ({ board, source, task: byTask.get(worker.task_id), worker }))
  })

  sourceWorkerEntries.sort(
    (left, right) =>
      left.board.localeCompare(right.board) ||
      left.worker.task_id.localeCompare(right.worker.task_id) ||
      left.worker.run_id - right.worker.run_id
  )

  for (const { board, source, task, worker } of sourceWorkerEntries) {
    const assignee = clean(task?.assignee)
    const workerProfile = clean(worker.profile)
    const aliases = [agentRunAlias(board, worker.task_id, worker.run_id)]
    const workerSessionId = clean(worker.worker_session_id)

    if (workerSessionId) {
      aliases.push(agentSessionAlias(profile, workerSessionId))
    }

    if (assignee && (!workerProfile || workerProfile === assignee)) {
      aliases.push(agentAssigneeAlias(board, worker.task_id, assignee))
    }

    const agentNodeId = upsertAgent({
      aliases,
      board,
      identity: workerSessionId || `run:${worker.run_id}`,
      label: workerProfile || assignee || 'Agent',
      priority: 1,
      revision: finiteMax(worker.latest_event_id, sourceRevision(source)),
      status: normalizedStatus(worker.status || worker.outcome || worker.task_status),
      taskId: worker.task_id
    })

    connectAgent(agentNodeId, taskByKey.get(taskKey(board, worker.task_id))?.nodeId)
  }

  const boardsForTask = (taskId: string): string[] =>
    Array.from(new Set(taskContexts.filter(context => context.task.id === taskId).map(context => context.board))).sort()

  const liveActivities = [...input.loopagents].sort(
    (left, right) =>
      clean(left.board).localeCompare(clean(right.board)) ||
      left.taskId.localeCompare(right.taskId) ||
      (left.runId || 0) - (right.runId || 0) ||
      left.id.localeCompare(right.id)
  )

  const currentToolActivityAtByTask = new Map<string, number>()

  for (const activity of liveActivities) {
    const candidateBoards = boardsForTask(activity.taskId)
    const board = clean(activity.board) || (candidateBoards.length === 1 ? candidateBoards[0] : 'unscoped')
    const context = taskByKey.get(taskKey(board, activity.taskId))
    const assignee = context?.assignee || ''
    const activityProfile = clean(activity.profile)
    const workerSessionId = clean(activity.workerSessionId)
    const aliases = [agentActivityAlias(board, activity.id)]
    const currentTool = clean(activity.currentTool)

    const previousCurrentToolActivityAt = context
      ? (currentToolActivityAtByTask.get(context.nodeId) ?? Number.NEGATIVE_INFINITY)
      : Number.NEGATIVE_INFINITY

    if (context && currentTool && activity.updatedAt >= previousCurrentToolActivityAt) {
      const taskNode = nodes.get(context.nodeId)

      if (taskNode) {
        nodes.set(context.nodeId, {
          ...taskNode,
          currentTool: loopToolLabel(currentTool)
        })
        currentToolActivityAtByTask.set(context.nodeId, activity.updatedAt)
      }
    }

    if (activity.runId !== undefined) {
      aliases.push(agentRunAlias(board, activity.taskId, activity.runId))
    }

    if (workerSessionId) {
      aliases.push(agentSessionAlias(profile, workerSessionId))
    }

    if (assignee && (!activityProfile || activityProfile === assignee)) {
      aliases.push(agentAssigneeAlias(board, activity.taskId, assignee))
    }

    const agentNodeId = upsertAgent({
      aliases,
      board,
      identity: workerSessionId || (activity.runId !== undefined ? `run:${activity.runId}` : activity.id),
      label: activityProfile || assignee || clean(activity.title) || 'Agent',
      priority: 2,
      revision: finiteMax(activity.revision, activity.latestTaskEventId, activity.sequence) ?? activity.updatedAt,
      status: normalizedStatus(activity.status || activity.taskStatus),
      taskId: activity.taskId
    })

    connectAgent(agentNodeId, context?.nodeId)

    for (const path of activity.filesWritten || []) {
      connectArtifact(agentNodeId, path)
    }
  }

  const subagentNodeIdById = new Map<string, string>()
  const subagents = [...input.subagents].sort((left, right) => left.id.localeCompare(right.id))

  for (const subagent of subagents) {
    const childSessionId = clean(subagent.sessionId)
    const aliases = [subagentAlias(subagent.id)]

    if (childSessionId) {
      aliases.push(agentSessionAlias(profile, childSessionId))
    }

    const agentNodeId = upsertAgent({
      aliases,
      board: 'session',
      identity: childSessionId || subagent.id,
      label: clean(subagent.goal) || 'Subagent',
      priority: 2,
      revision: subagent.updatedAt,
      status: normalizedStatus(subagent.status),
      taskId: input.session.id
    })

    subagentNodeIdById.set(subagent.id, agentNodeId)

    for (const path of subagent.filesWritten) {
      connectArtifact(agentNodeId, path)
    }
  }

  for (const subagent of subagents) {
    const agentNodeId = subagentNodeIdById.get(subagent.id)

    if (!agentNodeId) {
      continue
    }

    const parentNodeId = subagent.parentId ? subagentNodeIdById.get(subagent.parentId) : undefined

    if (parentNodeId) {
      putEdge('delegated_to', parentNodeId, agentNodeId)
    } else {
      connectAgent(agentNodeId)
    }
  }

  return {
    edges: [...edges.values()].sort((left, right) => left.id.localeCompare(right.id)),
    nodes: [...nodes.values()].sort((left, right) => left.id.localeCompare(right.id)),
    rootId: sessionNodeId
  }
}

/** Merge independently-built session graphs into one profile-wide topology. */
export function buildGlobalLiveGraph(inputs: readonly SessionLiveGraphInput[]): LiveGraphSnapshot {
  const nodes = new Map<string, LiveGraphNode>()
  const edges = new Map<string, LiveGraphEdge>()
  let rootId = ''

  for (const input of inputs) {
    const graph = buildSessionLiveGraph(input)
    rootId ||= graph.rootId || ''

    for (const node of graph.nodes) {
      const previous = nodes.get(node.id)

      const useIncoming =
        !previous ||
        (node.revision !== undefined && (previous.revision === undefined || node.revision >= previous.revision))

      nodes.set(node.id, {
        ...(previous || {}),
        ...(useIncoming ? node : {}),
        revision: finiteMax(previous?.revision, node.revision)
      } as LiveGraphNode)
    }

    for (const edge of graph.edges) {
      edges.set(edge.id, edge)
    }
  }

  return {
    edges: [...edges.values()].sort((left, right) => left.id.localeCompare(right.id)),
    nodes: [...nodes.values()].sort((left, right) => left.id.localeCompare(right.id)),
    ...(rootId ? { rootId } : {})
  }
}

const pulseKindForStatus = (status: string | undefined): LiveGraphPulseKind => {
  if (status === 'running' || status === 'active' || status === 'in_progress' || status === 'claimed') {
    return 'activated'
  }

  if (status === 'completed' || status === 'done' || status === 'success' || status === 'succeeded') {
    return 'completed'
  }

  if (status === 'blocked' || status === 'needs_review' || status === 'review_required') {
    return 'blocked'
  }

  if (status === 'failed' || status === 'error' || status === 'interrupted') {
    return 'failed'
  }

  return 'status_changed'
}

const pulseForEdge = (kind: LiveGraphPulseKind, edge: LiveGraphEdge, suffix = ''): LiveGraphPulse => ({
  edgeId: edge.id,
  id: `pulse:${kind}:${edge.id}${suffix ? `:${encodeIdPart(suffix)}` : ''}`,
  kind,
  sourceId: edge.sourceId,
  targetId: edge.targetId
})

const statusRevisionAdvanced = (previous: LiveGraphNode, next: LiveGraphNode): boolean => {
  if (previous.revision === undefined) {
    return true
  }

  return next.revision !== undefined && next.revision > previous.revision
}

/** Compare semantic snapshots. Timestamps and revision-only changes intentionally never fire. */
export function detectLiveGraphPulses(
  previous: LiveGraphSnapshot | null | undefined,
  next: LiveGraphSnapshot
): LiveGraphPulse[] {
  if (!previous) {
    return []
  }

  const previousNodes = new Map(previous.nodes.map(node => [node.id, node]))
  const previousEdges = new Set(previous.edges.map(edge => edge.id))
  const incoming = new Map<string, LiveGraphEdge[]>()
  const outgoing = new Map<string, LiveGraphEdge[]>()

  for (const edge of next.edges) {
    const entries = incoming.get(edge.targetId) || []
    entries.push(edge)
    incoming.set(edge.targetId, entries)

    const outgoingEntries = outgoing.get(edge.sourceId) || []
    outgoingEntries.push(edge)
    outgoing.set(edge.sourceId, outgoingEntries)
  }

  for (const entries of [...incoming.values(), ...outgoing.values()]) {
    entries.sort((left, right) => left.id.localeCompare(right.id))
  }

  const pulses = new Map<string, LiveGraphPulse>()

  const addPulse = (pulse: LiveGraphPulse): void => {
    pulses.set(pulse.id, pulse)
  }

  const incomingOfKind = (nodeId: string, kind: LiveGraphEdgeKind): LiveGraphEdge | undefined =>
    incoming.get(nodeId)?.find(edge => edge.kind === kind)

  const outgoingOfKind = (nodeId: string, kind: LiveGraphEdgeKind): LiveGraphEdge | undefined =>
    outgoing.get(nodeId)?.find(edge => edge.kind === kind)

  const taskPulse = (kind: LiveGraphPulseKind, nodeId: string, suffix = ''): LiveGraphPulse | null => {
    const rootEdge = incomingOfKind(nodeId, 'contains')

    if (rootEdge) {
      return pulseForEdge(kind, rootEdge, suffix)
    }

    const parentEdge = outgoingOfKind(nodeId, 'depends_on')

    if (!parentEdge) {
      return null
    }

    return {
      edgeId: parentEdge.id,
      id: `pulse:${kind}:${parentEdge.id}${suffix ? `:${encodeIdPart(suffix)}` : ''}`,
      kind,
      sourceId: parentEdge.targetId,
      targetId: parentEdge.sourceId
    }
  }

  for (const node of next.nodes) {
    if (previousNodes.has(node.id)) {
      continue
    }

    if (node.kind === 'task') {
      const pulse = taskPulse('task_added', node.id)

      if (pulse) {
        addPulse(pulse)
      }
    } else if (node.kind === 'artifact') {
      const edge = incomingOfKind(node.id, 'produced')

      if (edge) {
        addPulse(pulseForEdge('produced', edge))
      }
    }
  }

  for (const edge of next.edges) {
    if (previousEdges.has(edge.id)) {
      continue
    }

    if (edge.kind === 'delegated_to') {
      addPulse(pulseForEdge('delegated', edge))
    } else if (edge.kind === 'produced' && previousNodes.has(edge.targetId)) {
      addPulse(pulseForEdge('produced', edge))
    }
  }

  for (const node of next.nodes) {
    const before = previousNodes.get(node.id)

    if (
      !before ||
      (node.kind !== 'agent' && node.kind !== 'task') ||
      !node.status ||
      before.status === node.status ||
      !statusRevisionAdvanced(before, node)
    ) {
      continue
    }

    const kind = pulseKindForStatus(node.status)

    const pulse =
      node.kind === 'task'
        ? taskPulse(kind, node.id, node.status)
        : (() => {
            const edge = incomingOfKind(node.id, 'delegated_to') || incoming.get(node.id)?.[0]

            return edge ? pulseForEdge(kind, edge, node.status) : null
          })()

    if (pulse) {
      addPulse(pulse)
    }
  }

  return [...pulses.values()].sort((left, right) => left.id.localeCompare(right.id))
}
