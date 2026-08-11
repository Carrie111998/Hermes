/**
 * Visual workflow builder — pure-SVG DAG canvas (no graph library):
 * pan/zoom, node drag, connect mode with cycle detection, Kahn auto-layout,
 * and live per-node status while a run is in flight (server-driven via the
 * workflow query, invalidated by the /events socket).
 */
import {
  Badge,
  Button,
  Input,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Textarea,
  useMutation,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { type PointerEvent as RPointerEvent, useEffect, useMemo, useRef, useState } from 'react'

import { fetchWorkflow, runsKey, runWorkflow, saveWorkflow, workflowKey } from './api'
import { autoLayout, edgeExists, hasCycle } from './dag'
import type { useCrewsI18n } from './i18n'
import type { CrewMember, WorkflowEdge, WorkflowTask } from './types'

const NODE_W = 180
const NODE_H = 72
const MARKER_ID = 'crews-arrowhead'

function statusTone(status?: string): string {
  switch (status) {
    case 'running':
      return 'var(--ui-accent)'

    case 'done':
      return 'var(--ui-success, #34d399)'

    case 'error':
      return 'var(--ui-danger, #f87171)'

    default:
      return 'var(--ui-stroke-secondary)'
  }
}

function newTaskId(): string {
  return `t${Math.random().toString(36).slice(2, 10)}`
}

function bezierPath(x1: number, y1: number, x2: number, y2: number): string {
  const dx = Math.max(40, Math.abs(x2 - x1) / 2)

  return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`
}

export function WorkflowBuilder({
  crewId,
  members,
  k
}: {
  crewId: string
  members: CrewMember[]
  k: ReturnType<typeof useCrewsI18n>
}) {
  const qc = useQueryClient()
  const { data } = useQuery({ queryKey: workflowKey(crewId), queryFn: () => fetchWorkflow(crewId) })
  const workflow = data?.workflow ?? null

  const [tasks, setTasks] = useState<WorkflowTask[]>([])
  const [edges, setEdges] = useState<WorkflowEdge[]>([])
  const [loaded, setLoaded] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [connectMode, setConnectMode] = useState(false)
  const [connectSource, setConnectSource] = useState<string | null>(null)
  const [connectError, setConnectError] = useState<string | null>(null)
  const [scale, setScale] = useState(1)
  const [pan, setPan] = useState({ x: 40, y: 40 })
  const [dirty, setDirty] = useState(false)

  const svgRef = useRef<SVGSVGElement | null>(null)
  const panRef = useRef<{ startX: number; startY: number; panX: number; panY: number } | null>(null)
  const dragRef = useRef<{ id: string; dx: number; dy: number; moved: boolean } | null>(null)

  // Hydrate local draft from the server workflow once.
  useEffect(() => {
    if (workflow && !loaded) {
      setTasks(workflow.tasks)
      setEdges(workflow.edges)
      setLoaded(true)
    }
  }, [workflow, loaded])

  // Non-passive wheel zoom (must not scroll the page).
  useEffect(() => {
    const svg = svgRef.current

    if (!svg) {return}

    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1
      setScale(s => Math.min(4, Math.max(0.25, s * factor)))
    }

    svg.addEventListener('wheel', onWheel, { passive: false })

    return () => svg.removeEventListener('wheel', onWheel)
  }, [])

  const memberById = useMemo(() => new Map(members.map(m => [m.id, m])), [members])

  const save = useMutation({
    mutationFn: () => saveWorkflow(crewId, tasks, edges),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: workflowKey(crewId) })
      setDirty(false)
    }
  })

  const run = useMutation({
    mutationFn: () => runWorkflow(crewId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: workflowKey(crewId) })
      qc.invalidateQueries({ queryKey: runsKey(crewId) })
    }
  })

  const canRun =
    tasks.length > 0 &&
    !hasCycle(
      tasks.map(t => t.id),
      edges
    )

  const addTask = () => {
    const offset = tasks.length * 12

    const task: WorkflowTask = {
      id: newTaskId(),
      label: '',
      prompt: '',
      assigneeId: null,
      x: 40 + offset,
      y: 40 + offset
    }

    setTasks([...tasks, task])
    setSelectedId(task.id)
    setDirty(true)
  }

  const updateTask = (id: string, patch: Partial<WorkflowTask>) => {
    setTasks(tasks.map(t => (t.id === id ? { ...t, ...patch } : t)))
    setDirty(true)
  }

  const removeTask = (id: string) => {
    setTasks(tasks.filter(t => t.id !== id))
    setEdges(edges.filter(e => e.from !== id && e.to !== id))
    setSelectedId(null)
    setDirty(true)
  }

  const tryConnect = (from: string, to: string) => {
    if (from === to) {return}

    if (edgeExists(edges, from, to)) {
      setConnectError('That dependency already exists')

      return
    }

    const nextEdges = [...edges, { from, to }]

    if (
      hasCycle(
        tasks.map(t => t.id),
        nextEdges
      )
    ) {
      setConnectError('That would create a cycle')

      return
    }

    setEdges(nextEdges)
    setConnectError(null)
    setDirty(true)
  }

  const layout = () => {
    setTasks(autoLayout(tasks, edges))
    setDirty(true)
  }

  const selected = selectedId ? tasks.find(t => t.id === selectedId) : null

  // ── pointer handlers ──────────────────────────────────────────────────────

  const onBackgroundPointerDown = (e: RPointerEvent<SVGSVGElement>) => {
    if (e.target !== e.currentTarget) {return}
    panRef.current = { startX: e.clientX, startY: e.clientY, panX: pan.x, panY: pan.y }
    ;(e.currentTarget as SVGSVGElement).setPointerCapture(e.pointerId)
  }

  const onBackgroundPointerMove = (e: RPointerEvent<SVGSVGElement>) => {
    if (!panRef.current) {return}
    setPan({
      x: panRef.current.panX + (e.clientX - panRef.current.startX),
      y: panRef.current.panY + (e.clientY - panRef.current.startY)
    })
  }

  const onBackgroundPointerUp = (e: RPointerEvent<SVGSVGElement>) => {
    panRef.current = null
    ;(e.currentTarget as SVGSVGElement).releasePointerCapture(e.pointerId)
  }

  const onNodePointerDown = (e: RPointerEvent<SVGGElement>, task: WorkflowTask) => {
    e.stopPropagation()

    if (connectMode) {
      return // connect mode: handled on click
    }

    dragRef.current = {
      id: task.id,
      dx: e.clientX - task.x * scale - pan.x,
      dy: e.clientY - task.y * scale - pan.y,
      moved: false
    }
    ;(e.currentTarget as SVGGElement).setPointerCapture(e.pointerId)
  }

  const onNodePointerMove = (e: RPointerEvent<SVGGElement>) => {
    const drag = dragRef.current

    if (!drag) {return}
    const x = (e.clientX - drag.dx - pan.x) / scale
    const y = (e.clientY - drag.dy - pan.y) / scale

    if (
      Math.abs(x - (tasks.find(t => t.id === drag.id)?.x ?? 0)) > 0.5 ||
      Math.abs(y - (tasks.find(t => t.id === drag.id)?.y ?? 0)) > 0.5
    ) {
      drag.moved = true
    }

    updateTask(drag.id, { x, y })
  }

  const onNodePointerUp = (e: RPointerEvent<SVGGElement>, task: WorkflowTask) => {
    const drag = dragRef.current
    dragRef.current = null
    ;(e.currentTarget as SVGGElement).releasePointerCapture(e.pointerId)

    if (drag?.moved) {return} // dragged — not a click

    if (connectMode) {
      if (connectSource && connectSource !== task.id) {
        tryConnect(connectSource, task.id)
        setConnectSource(null)
        setConnectMode(false)
      } else {
        setConnectSource(task.id)
      }

      return
    }

    setSelectedId(task.id)
  }

  // ── render ────────────────────────────────────────────────────────────────

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2">
      <div className="flex flex-wrap items-center gap-1.5">
        <Button onClick={addTask} size="sm" variant="outline">
          {k.addTask}
        </Button>
        <Button
          onClick={() => {
            setConnectMode(!connectMode)
            setConnectSource(null)
            setConnectError(null)
          }}
          size="sm"
          variant={connectMode ? 'default' : 'outline'}
        >
          {k.connect}
        </Button>
        <Button disabled={tasks.length === 0} onClick={layout} size="sm" variant="outline">
          {k.autoLayout}
        </Button>
        <span className="mx-1 h-4 w-px bg-(--ui-stroke-secondary)" />
        <Button disabled={!dirty || save.isPending} onClick={() => save.mutate()} size="sm" variant="outline">
          {save.isPending ? '…' : k.save}
        </Button>
        <Button disabled={!canRun || run.isPending} onClick={() => run.mutate()} size="sm">
          {run.isPending ? k.runningNow : k.runWorkflow}
        </Button>
        <span className="mx-1 h-4 w-px bg-(--ui-stroke-secondary)" />
        <Button onClick={() => setScale(s => Math.min(4, s * 1.25))} size="sm" variant="ghost">
          +
        </Button>
        <Button onClick={() => setScale(s => Math.max(0.25, s / 1.25))} size="sm" variant="ghost">
          −
        </Button>
        <span className="text-[0.6875rem] tabular-nums text-(--ui-text-tertiary)">{Math.round(scale * 100)}%</span>
        {connectMode && !connectSource && (
          <span className="text-[0.6875rem] text-(--ui-text-tertiary)">{k.connectHint}</span>
        )}
        {connectMode && connectSource && (
          <span className="text-[0.6875rem] text-(--ui-text-tertiary)">{k.connectHint}</span>
        )}
        {connectError && <span className="text-[0.6875rem] text-(--ui-danger,#f87171)">{connectError}</span>}
      </div>

      {tasks.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 rounded-lg border border-(--ui-stroke-secondary)">
          <span className="text-sm text-(--ui-text-secondary)">{k.workflowEmpty}</span>
          <span className="text-xs text-(--ui-text-tertiary)">{k.workflowEmptyBody}</span>
          <Button className="mt-2" onClick={addTask} size="sm" variant="outline">
            {k.addTask}
          </Button>
        </div>
      ) : (
        <div className="relative min-h-0 flex-1 overflow-hidden rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-surface-secondary)">
          <svg
            className="h-full w-full cursor-grab active:cursor-grabbing select-none"
            onPointerDown={onBackgroundPointerDown}
            onPointerMove={onBackgroundPointerMove}
            onPointerUp={onBackgroundPointerUp}
            ref={svgRef}
          >
            <defs>
              <marker
                id={MARKER_ID}
                markerHeight="7"
                markerWidth="7"
                orient="auto-start-reverse"
                refX="9"
                refY="5"
                viewBox="0 0 10 10"
              >
                <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--ui-stroke-secondary)" />
              </marker>
            </defs>

            <g transform={`translate(${pan.x} ${pan.y}) scale(${scale})`}>
              {edges.map((edge, i) => {
                const from = tasks.find(t => t.id === edge.from)
                const to = tasks.find(t => t.id === edge.to)

                if (!from || !to) {return null}
                const active = from.status === 'running' || to.status === 'running'

                return (
                  <g
                    className="cursor-pointer"
                    key={i}
                    onClick={() => {
                      setEdges(edges.filter((_, j) => j !== i))
                      setDirty(true)
                    }}
                  >
                    <path
                      d={bezierPath(from.x + NODE_W, from.y + NODE_H / 2, to.x, to.y + NODE_H / 2)}
                      fill="none"
                      markerEnd={`url(#${MARKER_ID})`}
                      stroke={active ? 'var(--ui-accent)' : 'var(--ui-stroke-secondary)'}
                      strokeWidth={active ? 2 : 1.25}
                    />
                    {/* Wide invisible hit area for edge deletion */}
                    <path
                      d={bezierPath(from.x + NODE_W, from.y + NODE_H / 2, to.x, to.y + NODE_H / 2)}
                      fill="none"
                      stroke="transparent"
                      strokeWidth={14}
                    />
                  </g>
                )
              })}

              {tasks.map(task => {
                const tone = statusTone(task.status)
                const isSelected = selectedId === task.id
                const isConnectSource = connectSource === task.id

                return (
                  <g
                    className="cursor-pointer"
                    key={task.id}
                    onPointerDown={e => onNodePointerDown(e, task)}
                    onPointerMove={onNodePointerMove}
                    onPointerUp={e => onNodePointerUp(e, task)}
                    transform={`translate(${task.x} ${task.y})`}
                  >
                    <rect
                      fill="var(--ui-surface)"
                      height={NODE_H}
                      rx={8}
                      stroke={isConnectSource ? 'var(--ui-accent)' : tone}
                      strokeWidth={isSelected ? 2 : 1.25}
                      width={NODE_W}
                    />
                    <text className="pointer-events-none" fill="var(--ui-text-primary)" fontSize={12} x={10} y={18}>
                      {task.label || '(untitled)'}
                    </text>
                    <text className="pointer-events-none" fill="var(--ui-text-tertiary)" fontSize={10} x={10} y={36}>
                      {task.assigneeId ? (memberById.get(task.assigneeId)?.displayName ?? 'member') : k.allMembers}
                    </text>
                    <text className="pointer-events-none" fill="var(--ui-text-tertiary)" fontSize={10} x={10} y={54}>
                      {task.status ?? 'idle'}
                    </text>
                    {isSelected && (
                      <text
                        className="pointer-events-none"
                        fill="var(--ui-text-tertiary)"
                        fontSize={12}
                        textAnchor="end"
                        x={NODE_W - 10}
                        y={18}
                      >
                        ✕
                      </text>
                    )}
                  </g>
                )
              })}
            </g>
          </svg>

          {/* Right-side task editor panel */}
          {selected && (
            <div className="absolute inset-y-0 right-0 flex w-[300px] flex-col gap-2 overflow-y-auto border-l border-(--ui-stroke-secondary) bg-(--ui-surface) p-3">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium text-(--ui-text-primary)">{k.taskLabel}</span>
                <Button onClick={() => removeTask(selected.id)} size="sm" variant="ghost">
                  {k.delete}
                </Button>
              </div>
              <Input
                onChange={e => updateTask(selected.id, { label: e.target.value })}
                placeholder={k.taskLabelPlaceholder}
                value={selected.label}
              />
              <span className="text-xs text-(--ui-text-secondary)">{k.taskPrompt}</span>
              <Textarea
                onChange={e => updateTask(selected.id, { prompt: e.target.value })}
                placeholder={k.taskPromptPlaceholder}
                rows={5}
                value={selected.prompt}
              />
              <span className="text-xs text-(--ui-text-secondary)">{k.assignee}</span>
              <Select
                onValueChange={value => updateTask(selected.id, { assigneeId: value === 'all' ? null : value })}
                value={selected.assigneeId ?? 'all'}
              >
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">{k.allMembers}</SelectItem>
                  {members.map(m => (
                    <SelectItem key={m.id} value={m.id}>
                      {m.displayName}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <span className="text-xs text-(--ui-text-secondary)">{k.dependencies}</span>
              <div className="flex flex-wrap gap-1">
                {edges
                  .filter(e => e.to === selected.id)
                  .map(e => {
                    const dep = tasks.find(t => t.id === e.from)

                    return (
                      <Badge key={e.from} variant="outline">
                        {dep?.label || e.from.slice(0, 8)}
                      </Badge>
                    )
                  })}
                {edges.filter(e => e.to === selected.id).length === 0 && (
                  <span className="text-xs text-(--ui-text-tertiary)">—</span>
                )}
              </div>
              {selected.status && (
                <span className="text-xs text-(--ui-text-secondary)">
                  {k.status}: <Badge variant="outline">{selected.status}</Badge>
                </span>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
