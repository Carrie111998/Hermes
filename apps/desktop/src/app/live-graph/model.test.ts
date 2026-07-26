import { describe, expect, it } from 'vitest'

import type { TenantLoopSource } from '@/app/chat/loop-state'
import type { LoopagentActivity } from '@/store/loopagents'
import type { SubagentProgress } from '@/store/subagents'

import {
  buildGlobalLiveGraph,
  buildSessionLiveGraph,
  detectLiveGraphPulses,
  liveGraphNodeId,
  type SessionLiveGraphInput
} from './model'

const baseInput = (overrides: Partial<SessionLiveGraphInput> = {}): SessionLiveGraphInput => ({
  loopagents: [],
  profile: 'work',
  session: { cwd: '/repo', id: 'session-1', title: 'Ship Graph View' },
  sources: [],
  subagents: [],
  ...overrides
})

const taskSource = (board: string, tasks: NonNullable<TenantLoopSource['tasks']>, revision = 1): TenantLoopSource => ({
  board,
  latest_event_id: revision,
  links: [],
  session_id: 'session-1',
  tasks,
  workflow_id: 'workflow-1',
  workflow_ids: ['workflow-1']
})

const loopagent = (overrides: Partial<LoopagentActivity> = {}): LoopagentActivity => ({
  board: 'alpha',
  id: 'activity-1',
  kind: 'worker',
  parentTaskIds: [],
  profile: 'builder',
  revision: 3,
  runId: 7,
  sourceEvent: 'kanban.worker.progress',
  status: 'running',
  taskId: 'task-1',
  title: 'Build graph',
  updatedAt: 100,
  workerSessionId: 'worker-session-1',
  workflowId: 'workflow-1',
  ...overrides
})

const subagent = (overrides: Partial<SubagentProgress> = {}): SubagentProgress => ({
  filesRead: [],
  filesWritten: [],
  goal: 'Build graph',
  id: 'subagent-1',
  parentId: null,
  startedAt: 10,
  status: 'running',
  stream: [],
  taskCount: 1,
  taskIndex: 0,
  updatedAt: 100,
  ...overrides
})

describe('buildSessionLiveGraph', () => {
  it('keeps task details available for graph selection', () => {
    const graph = buildSessionLiveGraph(
      baseInput({
        sources: [
          taskSource('alpha', [
            {
              assignee: 'builder',
              body: 'Implement the selected task inspector.',
              completed_at: 303,
              created_at: 101,
              id: 'task-1',
              latest_summary: 'Inspector wiring is complete.',
              priority: 2,
              result: 'Focused tests passed.',
              started_at: 202,
              status: 'done',
              title: 'Show task details'
            }
          ])
        ]
      })
    )

    expect(graph.nodes.find(node => node.kind === 'task')).toMatchObject({
      assignee: 'builder',
      board: 'alpha',
      completedAt: 303,
      createdAt: 101,
      detail: 'Implement the selected task inspector.',
      entityId: 'task-1',
      priority: 2,
      result: 'Focused tests passed.',
      startedAt: 202,
      summary: 'Inspector wiring is complete.',
      workflowId: 'workflow-1'
    })
  })

  it('reuses the composer tool-call lookup for task nodes', () => {
    const graph = buildSessionLiveGraph(
      baseInput({
        sources: [
          taskSource('alpha', [
            {
              id: 'direct',
              status: 'running',
              title: 'Direct worker tool',
              worker_activity: {
                current_tool: 'kanban_block',
                run_id: 1,
                task_id: 'direct'
              }
            },
            {
              id: 'event',
              status: 'running',
              title: 'Recent event tool',
              worker_activity: {
                recent_task_events: [{ payload: { tool_name: 'write_file' } }],
                run_id: 2,
                task_id: 'event'
              }
            },
            {
              id: 'metadata',
              latest_run: { metadata: { last_tool: 'review_diff' } },
              status: 'todo',
              title: 'Latest run tool'
            }
          ])
        ]
      })
    )

    expect(
      Object.fromEntries(graph.nodes.filter(node => node.kind === 'task').map(node => [node.entityId, node.currentTool]))
    ).toEqual({
      direct: 'Kanban Block',
      event: 'Write File',
      metadata: 'Review Diff'
    })
  })

  it('uses the newest live tool activity without changing the task revision', () => {
    const graph = buildSessionLiveGraph(
      baseInput({
        loopagents: [
          loopagent({
            currentTool: 'search_files',
            id: 'newer-activity',
            revision: 4,
            runId: 7,
            updatedAt: 200
          }),
          loopagent({
            currentTool: 'write_file',
            id: 'older-activity',
            revision: 5,
            runId: 9,
            updatedAt: 100
          })
        ],
        sources: [
          taskSource(
            'alpha',
            [
              {
                id: 'task-1',
                status: 'running',
                title: 'Live tool activity',
                worker_activity: {
                  current_tool: 'terminal',
                  run_id: 1,
                  task_id: 'task-1'
                }
              }
            ],
            50
          )
        ]
      })
    )

    expect(graph.nodes.find(node => node.kind === 'task')).toMatchObject({
      currentTool: 'Search Files',
      revision: 50
    })
  })

  it('uses first-class workflow titles from overview sources', () => {
    const source = taskSource('alpha', [{ id: 'task-1', status: 'queued', title: 'Build graph' }])
    source.workflows = [{ created_at: 99, id: 'workflow-1', status: 'open', title: 'Ship the product' }]

    const graph = buildSessionLiveGraph(baseInput({ sources: [source] }))

    expect(graph.nodes.find(node => node.kind === 'workflow')).toMatchObject({
      createdAt: 99,
      label: 'Ship the product',
      status: 'open'
    })
  })

  it('keeps identical workflow and task ids isolated by board', () => {
    const graph = buildSessionLiveGraph(
      baseInput({
        sources: [
          taskSource('alpha', [{ id: 'shared', status: 'queued', title: 'Alpha task' }]),
          taskSource('beta', [{ id: 'shared', status: 'queued', title: 'Beta task' }])
        ]
      })
    )

    const taskIds = graph.nodes.filter(node => node.kind === 'task').map(node => node.id)
    const workflowIds = graph.nodes.filter(node => node.kind === 'workflow').map(node => node.id)

    expect(taskIds).toEqual([
      liveGraphNodeId('task', 'work', 'alpha', 'shared'),
      liveGraphNodeId('task', 'work', 'beta', 'shared')
    ])
    expect(workflowIds).toEqual([
      liveGraphNodeId('workflow', 'work', 'alpha', 'workflow-1'),
      liveGraphNodeId('workflow', 'work', 'beta', 'workflow-1')
    ])
  })

  it('builds the session topology and dedupes workers and their artifacts', () => {
    const worker = {
      latest_event_id: 3,
      profile: 'builder',
      run_id: 7,
      status: 'running',
      task_id: 'task-1',
      worker_session_id: 'worker-session-1'
    }

    const source = taskSource('alpha', [
      { id: 'parent', status: 'done', title: 'Plan' },
      {
        assignee: 'builder',
        id: 'task-1',
        included_parent_ids: ['parent'],
        status: 'running',
        title: 'Build graph',
        worker_activity: worker
      }
    ])

    source.workers = [worker]

    const graph = buildSessionLiveGraph(
      baseInput({
        loopagents: [loopagent({ filesWritten: ['/tmp/result.json', '/tmp/result.json'] })],
        project: { boardSlug: 'alpha', id: 'project-1', name: 'Hermes' },
        sources: [source],
        subagents: [
          subagent({
            filesWritten: ['/tmp/result.json', '/tmp/notes.md'],
            sessionId: 'worker-session-1'
          })
        ]
      })
    )

    expect(graph.nodes.filter(node => node.kind === 'agent')).toHaveLength(1)
    expect(graph.nodes.filter(node => node.kind === 'artifact').map(node => node.path)).toEqual([
      '/tmp/notes.md',
      '/tmp/result.json'
    ])
    expect(new Set(graph.edges.map(edge => edge.kind))).toEqual(
      new Set(['contains', 'delegated_to', 'depends_on', 'produced'])
    )
    expect(graph.edges.filter(edge => edge.kind === 'produced')).toHaveLength(2)

    const parentId = liveGraphNodeId('task', 'work', 'alpha', 'parent')
    const childId = liveGraphNodeId('task', 'work', 'alpha', 'task-1')
    const sessionId = liveGraphNodeId('session', 'work', 'session-1')
    const workflowId = liveGraphNodeId('workflow', 'work', 'alpha', 'workflow-1')
    const agentId = graph.nodes.find(node => node.kind === 'agent')!.id

    expect(graph.edges).toContainEqual(
      expect.objectContaining({ kind: 'contains', sourceId: workflowId, targetId: parentId })
    )
    expect(graph.edges).toContainEqual(
      expect.objectContaining({ kind: 'depends_on', sourceId: childId, targetId: parentId })
    )
    expect(graph.edges).not.toContainEqual(
      expect.objectContaining({ kind: 'contains', sourceId: workflowId, targetId: childId })
    )
    expect(graph.edges).toContainEqual(
      expect.objectContaining({ kind: 'delegated_to', sourceId: childId, targetId: agentId })
    )
    expect(
      graph.edges.some(
        edge =>
          (edge.sourceId === sessionId && edge.targetId === agentId) ||
          (edge.sourceId === agentId && edge.targetId === sessionId)
      )
    ).toBe(false)
  })

  it('builds one structural hierarchy from every task relationship field', () => {
    const source = taskSource('alpha', [
      {
        id: 'root',
        included_child_ids: ['included-child'],
        links: { children: ['linked-child'] },
        status: 'queued',
        title: 'Root'
      },
      { id: 'included-child', status: 'queued', title: 'Included child' },
      { id: 'linked-child', status: 'queued', title: 'Linked child' },
      {
        id: 'included-parent',
        included_parent_ids: ['root'],
        status: 'queued',
        title: 'Included parent reference'
      },
      { id: 'linked-parent', links: { parents: ['root'] }, status: 'queued', title: 'Linked parent reference' },
      { id: 'source-link', status: 'queued', title: 'Source link' }
    ])

    source.links = [{ child_id: 'source-link', parent_id: 'root' }]

    const graph = buildSessionLiveGraph(baseInput({ sources: [source] }))
    const workflowId = liveGraphNodeId('workflow', 'work', 'alpha', 'workflow-1')
    const rootId = liveGraphNodeId('task', 'work', 'alpha', 'root')

    const childIds = ['included-child', 'linked-child', 'included-parent', 'linked-parent', 'source-link'].map(id =>
      liveGraphNodeId('task', 'work', 'alpha', id)
    )

    expect(
      graph.edges.filter(edge => edge.kind === 'contains' && edge.sourceId === workflowId).map(edge => edge.targetId)
    ).toEqual([rootId])

    for (const childId of childIds) {
      expect(graph.edges).toContainEqual(
        expect.objectContaining({ kind: 'depends_on', sourceId: childId, targetId: rootId })
      )
    }
  })

  it('removes transitive same-workflow task shortcuts', () => {
    const source = taskSource('alpha', [
      { id: 'task-a', status: 'queued', title: 'Task A' },
      { id: 'task-b', status: 'queued', title: 'Task B' },
      { id: 'task-c', status: 'queued', title: 'Task C' }
    ])

    source.links = [
      { child_id: 'task-b', parent_id: 'task-a' },
      { child_id: 'task-c', parent_id: 'task-b' },
      { child_id: 'task-c', parent_id: 'task-a' }
    ]

    const graph = buildSessionLiveGraph(baseInput({ sources: [source] }))
    const workflowId = liveGraphNodeId('workflow', 'work', 'alpha', 'workflow-1')
    const taskAId = liveGraphNodeId('task', 'work', 'alpha', 'task-a')
    const taskBId = liveGraphNodeId('task', 'work', 'alpha', 'task-b')
    const taskCId = liveGraphNodeId('task', 'work', 'alpha', 'task-c')

    expect(graph.edges).toContainEqual(
      expect.objectContaining({ kind: 'contains', sourceId: workflowId, targetId: taskAId })
    )
    expect(graph.edges).toContainEqual(
      expect.objectContaining({ kind: 'depends_on', sourceId: taskBId, targetId: taskAId })
    )
    expect(graph.edges).toContainEqual(
      expect.objectContaining({ kind: 'depends_on', sourceId: taskCId, targetId: taskBId })
    )
    expect(graph.edges).not.toContainEqual(
      expect.objectContaining({ kind: 'depends_on', sourceId: taskCId, targetId: taskAId })
    )
  })

  it('keeps tasks attached to their workflow when parents are missing, external, or cross-workflow', () => {
    const source = taskSource('alpha', [
      { id: 'parent', status: 'queued', title: 'Workflow one parent', workflow_id: 'workflow-1' },
      {
        external_parent_tasks: [{ id: 'external' }],
        id: 'external-child',
        included_parent_ids: ['external'],
        status: 'queued',
        title: 'External parent child',
        workflow_id: 'workflow-1'
      },
      {
        id: 'missing-child',
        included_parent_ids: ['missing'],
        status: 'queued',
        title: 'Missing parent child',
        workflow_id: 'workflow-1'
      },
      {
        id: 'cross-workflow-child',
        included_parent_ids: ['parent'],
        status: 'queued',
        title: 'Cross workflow child',
        workflow_id: 'workflow-2'
      }
    ])

    source.workflow_ids = ['workflow-1', 'workflow-2']

    const graph = buildSessionLiveGraph(baseInput({ sources: [source] }))
    const workflowOneId = liveGraphNodeId('workflow', 'work', 'alpha', 'workflow-1')
    const workflowTwoId = liveGraphNodeId('workflow', 'work', 'alpha', 'workflow-2')
    const parentId = liveGraphNodeId('task', 'work', 'alpha', 'parent')
    const externalChildId = liveGraphNodeId('task', 'work', 'alpha', 'external-child')
    const missingChildId = liveGraphNodeId('task', 'work', 'alpha', 'missing-child')
    const crossWorkflowChildId = liveGraphNodeId('task', 'work', 'alpha', 'cross-workflow-child')

    expect(graph.edges).toContainEqual(
      expect.objectContaining({ kind: 'contains', sourceId: workflowOneId, targetId: parentId })
    )
    expect(graph.edges).toContainEqual(
      expect.objectContaining({ kind: 'contains', sourceId: workflowOneId, targetId: externalChildId })
    )
    expect(graph.edges).toContainEqual(
      expect.objectContaining({ kind: 'contains', sourceId: workflowOneId, targetId: missingChildId })
    )
    expect(graph.edges).toContainEqual(
      expect.objectContaining({ kind: 'contains', sourceId: workflowTwoId, targetId: crossWorkflowChildId })
    )
    expect(graph.edges).toContainEqual(
      expect.objectContaining({ kind: 'depends_on', sourceId: crossWorkflowChildId, targetId: parentId })
    )
  })

  it('anchors each rootless task component to its workflow deterministically', () => {
    const graph = buildSessionLiveGraph(
      baseInput({
        sources: [
          taskSource('alpha', [
            { id: 'cycle-a', included_parent_ids: ['cycle-b'], status: 'queued', title: 'Cycle A' },
            { id: 'cycle-b', included_parent_ids: ['cycle-a'], status: 'queued', title: 'Cycle B' }
          ])
        ]
      })
    )

    const workflowId = liveGraphNodeId('workflow', 'work', 'alpha', 'workflow-1')
    const cycleAId = liveGraphNodeId('task', 'work', 'alpha', 'cycle-a')
    const cycleBId = liveGraphNodeId('task', 'work', 'alpha', 'cycle-b')

    expect(
      graph.edges.filter(edge => edge.kind === 'contains' && edge.sourceId === workflowId).map(edge => edge.targetId)
    ).toEqual([cycleAId])
    expect(graph.edges).toContainEqual(
      expect.objectContaining({ kind: 'depends_on', sourceId: cycleBId, targetId: cycleAId })
    )
    expect(graph.edges).toContainEqual(
      expect.objectContaining({ kind: 'depends_on', sourceId: cycleAId, targetId: cycleBId })
    )
  })

  it('uses the session only as a fallback for activity without a matching task', () => {
    const graph = buildSessionLiveGraph(baseInput({ loopagents: [loopagent({ taskId: 'missing-task' })] }))
    const sessionId = liveGraphNodeId('session', 'work', 'session-1')
    const agentId = graph.nodes.find(node => node.kind === 'agent')!.id

    expect(graph.edges).toContainEqual(
      expect.objectContaining({ kind: 'delegated_to', sourceId: sessionId, targetId: agentId })
    )
  })
})

describe('buildGlobalLiveGraph', () => {
  it('keeps session clusters separate while merging shared project identity', () => {
    const first = baseInput({
      project: { boardSlug: 'alpha', id: 'project-1', name: 'Hermes' },
      session: { cwd: '/repo', id: 'session-1', title: 'First session' },
      sources: [taskSource('alpha', [{ id: 'task-1', status: 'running', title: 'First task' }])]
    })

    const second = baseInput({
      project: { boardSlug: 'alpha', id: 'project-1', name: 'Hermes' },
      session: { cwd: '/repo', id: 'session-2', title: 'Second session' },
      sources: [
        {
          ...taskSource('alpha', [{ id: 'task-2', status: 'queued', title: 'Second task' }]),
          workflow_id: 'workflow-2',
          workflow_ids: ['workflow-2']
        }
      ]
    })

    const graph = buildGlobalLiveGraph([first, second])

    expect(graph.nodes.filter(node => node.kind === 'session')).toHaveLength(2)
    expect(graph.nodes.filter(node => node.kind === 'project')).toHaveLength(1)
    expect(graph.edges.filter(edge => edge.sourceId === liveGraphNodeId('project', 'work', 'project-1'))).toHaveLength(
      2
    )
    expect(graph.rootId).toBe(liveGraphNodeId('session', 'work', 'session-1'))
  })

  it('preserves reduced immediate task links while merging sessions', () => {
    const source = taskSource('alpha', [
      { id: 'task-a', status: 'queued', title: 'Task A' },
      { id: 'task-b', status: 'queued', title: 'Task B' },
      { id: 'task-c', status: 'queued', title: 'Task C' }
    ])

    source.links = [
      { child_id: 'task-b', parent_id: 'task-a' },
      { child_id: 'task-c', parent_id: 'task-b' },
      { child_id: 'task-c', parent_id: 'task-a' }
    ]

    const graph = buildGlobalLiveGraph([baseInput({ sources: [source] })])
    const taskAId = liveGraphNodeId('task', 'work', 'alpha', 'task-a')
    const taskBId = liveGraphNodeId('task', 'work', 'alpha', 'task-b')
    const taskCId = liveGraphNodeId('task', 'work', 'alpha', 'task-c')

    expect(graph.edges).toContainEqual(
      expect.objectContaining({ kind: 'depends_on', sourceId: taskBId, targetId: taskAId })
    )
    expect(graph.edges).toContainEqual(
      expect.objectContaining({ kind: 'depends_on', sourceId: taskCId, targetId: taskBId })
    )
    expect(graph.edges).not.toContainEqual(
      expect.objectContaining({ kind: 'depends_on', sourceId: taskCId, targetId: taskAId })
    )
  })
})

describe('detectLiveGraphPulses', () => {
  it('keeps initial hydration, exact repeats, timestamp-only changes, and no-op revisions quiet', () => {
    const source = taskSource('alpha', [{ id: 'task-1', status: 'queued', title: 'Build graph' }], 1)
    const initial = buildSessionLiveGraph(baseInput({ loopagents: [loopagent()], sources: [source] }))

    const timestampOnly = buildSessionLiveGraph(
      baseInput({ loopagents: [loopagent({ updatedAt: 999 })], sources: [{ ...source, now: 999 }] })
    )

    const revisionOnly = buildSessionLiveGraph(
      baseInput({ loopagents: [loopagent({ revision: 4 })], sources: [{ ...source, latest_event_id: 2 }] })
    )

    expect(detectLiveGraphPulses(undefined, initial)).toEqual([])
    expect(detectLiveGraphPulses(initial, initial)).toEqual([])
    expect(detectLiveGraphPulses(initial, timestampOnly)).toEqual([])
    expect(detectLiveGraphPulses(initial, revisionOnly)).toEqual([])
  })

  it('emits one semantic pulse for each new task, delegation, and artifact', () => {
    const firstSource = taskSource('alpha', [{ id: 'task-1', status: 'queued', title: 'First' }], 1)

    const withTaskSource = taskSource(
      'alpha',
      [
        { id: 'task-1', status: 'queued', title: 'First' },
        { id: 'task-2', status: 'queued', title: 'Second' }
      ],
      2
    )

    const beforeTask = buildSessionLiveGraph(baseInput({ sources: [firstSource] }))
    const afterTask = buildSessionLiveGraph(baseInput({ sources: [withTaskSource] }))

    expect(detectLiveGraphPulses(beforeTask, afterTask).map(pulse => pulse.kind)).toEqual(['task_added'])

    const beforeDelegation = buildSessionLiveGraph(baseInput({ sources: [withTaskSource] }))
    const afterDelegation = buildSessionLiveGraph(baseInput({ sources: [withTaskSource], subagents: [subagent()] }))
    expect(detectLiveGraphPulses(beforeDelegation, afterDelegation).map(pulse => pulse.kind)).toEqual(['delegated'])

    const activity = loopagent({ filesWritten: [] })
    const beforeArtifact = buildSessionLiveGraph(baseInput({ loopagents: [activity], sources: [firstSource] }))

    const afterArtifact = buildSessionLiveGraph(
      baseInput({
        loopagents: [{ ...activity, filesWritten: ['/tmp/result.json'], updatedAt: 200 }],
        sources: [firstSource]
      })
    )

    const artifactPulses = detectLiveGraphPulses(beforeArtifact, afterArtifact)

    expect(artifactPulses).toHaveLength(1)
    expect(artifactPulses[0]).toMatchObject({ kind: 'produced' })
    expect(artifactPulses[0]?.sourceId).toMatch(/^agent:/)
    expect(artifactPulses[0]?.edgeId).toMatch(/^edge:produced:/)
    expect(artifactPulses[0]?.targetId).toMatch(/^artifact:/)
  })

  it('fires once for an advancing status and ignores stale status revisions', () => {
    const queued = buildSessionLiveGraph(
      baseInput({ sources: [taskSource('alpha', [{ id: 'task-1', status: 'queued', title: 'Build' }], 5)] })
    )

    const running = buildSessionLiveGraph(
      baseInput({ sources: [taskSource('alpha', [{ id: 'task-1', status: 'running', title: 'Build' }], 6)] })
    )

    const staleFailure = buildSessionLiveGraph(
      baseInput({ sources: [taskSource('alpha', [{ id: 'task-1', status: 'failed', title: 'Build' }], 4)] })
    )

    const pulses = detectLiveGraphPulses(queued, running)
    expect(pulses).toHaveLength(1)
    expect(pulses[0]).toMatchObject({ kind: 'activated' })
    expect(pulses[0]?.sourceId).toMatch(/^workflow:/)
    expect(pulses[0]?.edgeId).toMatch(/^edge:contains:/)
    expect(pulses[0]?.targetId).toMatch(/^task:/)
    expect(detectLiveGraphPulses(running, staleFailure)).toEqual([])
  })

  it('pulses descendant task changes from parent to child while roots pulse from the workflow', () => {
    const parentId = liveGraphNodeId('task', 'work', 'alpha', 'parent')
    const childId = liveGraphNodeId('task', 'work', 'alpha', 'child')
    const workflowId = liveGraphNodeId('workflow', 'work', 'alpha', 'workflow-1')

    const rootOnly = buildSessionLiveGraph(
      baseInput({ sources: [taskSource('alpha', [{ id: 'parent', status: 'running', title: 'Parent' }], 1)] })
    )

    const queuedChild = buildSessionLiveGraph(
      baseInput({
        sources: [
          taskSource(
            'alpha',
            [
              { id: 'parent', status: 'running', title: 'Parent' },
              { id: 'child', included_parent_ids: ['parent'], status: 'queued', title: 'Child' }
            ],
            2
          )
        ]
      })
    )

    const runningChild = buildSessionLiveGraph(
      baseInput({
        sources: [
          taskSource(
            'alpha',
            [
              { id: 'parent', status: 'running', title: 'Parent' },
              { id: 'child', included_parent_ids: ['parent'], status: 'running', title: 'Child' }
            ],
            3
          )
        ]
      })
    )

    expect(detectLiveGraphPulses(rootOnly, queuedChild)).toEqual([
      expect.objectContaining({ kind: 'task_added', sourceId: parentId, targetId: childId })
    ])
    expect(detectLiveGraphPulses(queuedChild, runningChild)).toEqual([
      expect.objectContaining({ kind: 'activated', sourceId: parentId, targetId: childId })
    ])
    expect(queuedChild.edges).not.toContainEqual(
      expect.objectContaining({ kind: 'contains', sourceId: workflowId, targetId: childId })
    )
  })
})
