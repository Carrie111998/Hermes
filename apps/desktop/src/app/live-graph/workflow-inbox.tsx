import { AnimatePresence, LayoutGroup, motion, useReducedMotion } from 'motion/react'
import { memo, useEffect, useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { useI18n } from '@/i18n'
import { formatAgo } from '@/lib/time'

import {
  LIVE_GRAPH_ATTENTION_STATUSES,
  LIVE_GRAPH_COMPLETED_STATUSES,
  LIVE_GRAPH_WAITING_STATUSES,
  type LiveGraphNode,
  normalizeLiveGraphStatus
} from './model'

const COMPLETED_PAGE_SIZE = 10
const COMPLETED_PAGE_INCREMENT = 25
const TASK_AGE_REFRESH_INTERVAL_MS = 60_000

type WorkflowInboxFilter = 'active' | 'all' | 'attention' | 'completed'

interface StatusPresentation {
  color: string
  icon: string
  rank: number
}

const DEFAULT_STATUS_PRESENTATION: StatusPresentation = {
  color: 'var(--ui-text-quaternary)',
  icon: 'circle-outline',
  rank: 4
}

const WAITING_STATUS_PRESENTATION: StatusPresentation = {
  color: 'var(--ui-purple)',
  icon: 'clock',
  rank: 3
}

const STATUS_PRESENTATION: Record<string, StatusPresentation> = {
  blocked: { color: 'var(--ui-yellow)', icon: 'warning', rank: 1 },
  closed: { color: 'var(--ui-green)', icon: 'pass-filled', rank: 4 },
  completed: { color: 'var(--ui-green)', icon: 'pass-filled', rank: 4 },
  failed: { color: 'var(--ui-red)', icon: 'error', rank: 2 },
  interrupted: { color: 'var(--ui-yellow)', icon: 'warning', rank: 1 },
  running: { color: 'var(--ui-cyan)', icon: 'sync', rank: 0 }
}

export interface LiveGraphWorkflowInboxProps {
  onSelectTask: (nodeId: string) => void
  tasks: readonly LiveGraphNode[]
  workflowScope: string
}

function clean(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function statusPresentation(status: string): StatusPresentation {
  return (
    STATUS_PRESENTATION[status] ||
    (LIVE_GRAPH_WAITING_STATUSES.has(status) ? WAITING_STATUS_PRESENTATION : DEFAULT_STATUS_PRESENTATION)
  )
}

function needsAttention(status: unknown): boolean {
  return LIVE_GRAPH_ATTENTION_STATUSES.has(normalizeLiveGraphStatus(status))
}

function taskSort(left: LiveGraphNode, right: LiveGraphNode): number {
  const rank = (task: LiveGraphNode) => statusPresentation(normalizeLiveGraphStatus(task.status)).rank

  return (
    rank(left) - rank(right) || (right.createdAt ?? 0) - (left.createdAt ?? 0) || left.label.localeCompare(right.label)
  )
}

function completedTaskSort(left: LiveGraphNode, right: LiveGraphNode): number {
  return (
    (right.completedAt ?? right.createdAt ?? 0) - (left.completedAt ?? left.createdAt ?? 0) ||
    left.label.localeCompare(right.label)
  )
}

interface TaskLifecycleTiming {
  timestampMs: number
}

function taskTimestampMs(value: number | undefined): number | null {
  if (value === undefined || !Number.isFinite(value) || value <= 0) {
    return null
  }

  const timestampMs = value < 1_000_000_000_000 ? value * 1000 : value

  return timestampMs <= 8_640_000_000_000_000 ? timestampMs : null
}

function taskLifecycleTiming(task: LiveGraphNode): TaskLifecycleTiming | null {
  const completedAt = taskTimestampMs(task.completedAt)
  const status = normalizeLiveGraphStatus(task.status)

  if (
    completedAt !== null &&
    (status === 'closed' || status === 'completed' || status === 'failed' || status === 'interrupted')
  ) {
    return { timestampMs: completedAt }
  }

  const startedAt = taskTimestampMs(task.startedAt)

  if (startedAt !== null) {
    return { timestampMs: startedAt }
  }

  const createdAt = taskTimestampMs(task.createdAt)

  return createdAt === null ? null : { timestampMs: createdAt }
}

function TaskStatus({ status }: { status: string }) {
  const { t } = useI18n()
  const normalized = normalizeLiveGraphStatus(status)
  const presentation = statusPresentation(normalized)
  const label = (t.liveGraph.statuses as Record<string, string>)[normalized] || status || t.liveGraph.statuses.unknown

  return (
    <span
      className="flex min-w-0 shrink-0 items-center gap-1.5 text-[0.625rem] text-(--ui-text-tertiary)"
      data-live-graph-task-status={normalized}
    >
      <Codicon className="shrink-0" name={presentation.icon} style={{ color: presentation.color }} />
      <span className="truncate">{label}</span>
    </span>
  )
}

interface TaskCardProps {
  nowMs: number
  onSelect: () => void
  reducedMotion: boolean
  task: LiveGraphNode
}

function TaskCard({ nowMs, onSelect, reducedMotion, task }: TaskCardProps) {
  const { t } = useI18n()
  const assignee = clean(task.assignee) || t.liveGraph.unassigned
  const currentTool = clean(task.currentTool)
  const supportingText = clean(task.summary) || clean(task.detail) || clean(task.result)
  const timing = taskLifecycleTiming(task)

  return (
    <motion.article
      className="relative min-w-0 overflow-hidden rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-bg-secondary) shadow-[0_1px_0_color-mix(in_srgb,var(--ui-text-primary)_4%,transparent)] transition-colors duration-150 hover:border-(--stroke-nous) hover:bg-(--ui-bg-tertiary) focus-within:border-(--ui-stroke-primary) motion-reduce:transition-none"
      data-live-graph-task-card={task.id}
      exit={reducedMotion ? { opacity: 1 } : { opacity: 0, y: 4 }}
      layout="position"
      layoutId={`live-graph-workflow-task:${task.id}`}
      transition={{ duration: reducedMotion ? 0 : 0.15, ease: 'easeOut' }}
    >
      <button
        aria-label={`${t.liveGraph.viewTask}: ${task.label}`}
        className="flex h-[8.5rem] w-full min-w-0 cursor-default flex-col border-0 bg-transparent px-3 py-2.5 text-left text-inherit outline-none"
        onClick={onSelect}
        type="button"
      >
        <span className="flex min-w-0 items-center justify-between gap-2">
          <span className="flex min-w-0 items-center gap-1.5 text-[0.625rem] font-medium text-(--ui-text-secondary)">
            <Codicon className="shrink-0 text-(--ui-text-tertiary)" name="account" />
            <span className="truncate">{assignee}</span>
          </span>
          <TaskStatus status={task.status || 'unknown'} />
        </span>
        <span
          className="mt-1.5 min-w-0 line-clamp-2 text-xs leading-4 font-semibold text-(--ui-text-primary)"
          data-live-graph-task-card-title
        >
          {task.label}
        </span>
        <span
          className="mt-1 h-8 min-w-0 line-clamp-2 text-[0.6875rem] leading-4 text-(--ui-text-secondary)"
          data-live-graph-task-card-description
        >
          {supportingText || '\u00a0'}
        </span>
        <span
          className="mt-auto flex min-w-0 items-center gap-2 text-[0.625rem] text-(--ui-text-tertiary)"
          data-live-graph-task-card-metadata
        >
          {currentTool ? (
            <>
              <Codicon className="shrink-0" name="tools" />
              <span className="min-w-0 truncate" data-live-graph-task-tool-call>
                {currentTool}
              </span>
            </>
          ) : null}
          {timing ? (
            <time
              className="ml-auto shrink-0"
              data-live-graph-task-age
              dateTime={new Date(timing.timestampMs).toISOString()}
            >
              {formatAgo(timing.timestampMs, t.agents, nowMs)}
            </time>
          ) : null}
        </span>
      </button>
    </motion.article>
  )
}

interface CompletedTaskRowProps {
  nowMs: number
  onSelect: () => void
  reducedMotion: boolean
  task: LiveGraphNode
}

function CompletedTaskRow({ nowMs, onSelect, reducedMotion, task }: CompletedTaskRowProps) {
  const { t } = useI18n()
  const timing = taskLifecycleTiming(task)
  const timingLabel = timing ? formatAgo(timing.timestampMs, t.agents, nowMs) : ''

  return (
    <motion.div
      className="relative min-w-0 rounded-md transition-colors duration-150 hover:bg-(--ui-bg-tertiary) focus-within:bg-(--ui-bg-tertiary) motion-reduce:transition-none"
      data-live-graph-completed-task={task.id}
      exit={reducedMotion ? { opacity: 1 } : { opacity: 0, y: -3 }}
      layout="position"
      layoutId={`live-graph-workflow-task:${task.id}`}
      transition={{ duration: reducedMotion ? 0 : 0.15, ease: 'easeOut' }}
    >
      <button
        aria-label={`${t.liveGraph.viewTask}: ${task.label}`}
        className="flex h-8 w-full min-w-0 cursor-default items-center gap-2 border-0 bg-transparent px-2 text-left text-inherit outline-none"
        onClick={onSelect}
        type="button"
      >
        <Codicon className="shrink-0 text-(--ui-text-tertiary)" name="checklist" />
        <span className="min-w-0 flex-1 truncate text-[0.6875rem] text-(--ui-text-secondary)">{task.label}</span>
        {timing ? (
          <time
            className="shrink-0 text-[0.625rem] text-(--ui-text-quaternary)"
            data-live-graph-task-age
            dateTime={new Date(timing.timestampMs).toISOString()}
          >
            {timingLabel}
          </time>
        ) : null}
      </button>
    </motion.div>
  )
}

export const LiveGraphWorkflowInbox = memo(function LiveGraphWorkflowInbox({
  onSelectTask,
  tasks,
  workflowScope
}: LiveGraphWorkflowInboxProps) {
  const { t } = useI18n()
  const reducedMotion = Boolean(useReducedMotion())
  const [filter, setFilter] = useState<WorkflowInboxFilter>('all')
  const [nowMs, setNowMs] = useState(() => Date.now())
  const [visibleCompletedCount, setVisibleCompletedCount] = useState(COMPLETED_PAGE_SIZE)

  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), TASK_AGE_REFRESH_INTERVAL_MS)

    return () => window.clearInterval(timer)
  }, [])

  const { activeTasks, attentionTasks, completedTasks } = useMemo(() => {
    const active: LiveGraphNode[] = []
    const attention: LiveGraphNode[] = []
    const completed: LiveGraphNode[] = []

    for (const task of tasks) {
      if (LIVE_GRAPH_COMPLETED_STATUSES.has(normalizeLiveGraphStatus(task.status))) {
        completed.push(task)
      } else if (needsAttention(task.status)) {
        attention.push(task)
      } else {
        active.push(task)
      }
    }

    return {
      activeTasks: active.sort(taskSort),
      attentionTasks: attention.sort(taskSort),
      completedTasks: completed.sort(completedTaskSort)
    }
  }, [tasks])

  const visibleCompletedTasks = completedTasks.slice(0, visibleCompletedCount)
  const hiddenCompletedCount = Math.max(0, completedTasks.length - visibleCompletedTasks.length)
  const showActiveTasks = filter === 'all' || filter === 'active'
  const showAttentionTasks = filter === 'all' || filter === 'attention'
  const showCompletedTasks = filter === 'all' || filter === 'completed'

  return (
    <LayoutGroup id={`live-graph-workflow-inbox:${workflowScope}`}>
      <div
        aria-label={t.liveGraph.taskInbox}
        className="grid min-w-0 gap-4 border-t border-(--ui-stroke-tertiary) px-3 py-3"
        data-testid="live-graph-workflow-inbox"
        role="region"
      >
        <div aria-label={t.liveGraph.taskInbox} className="flex min-w-0 flex-nowrap items-center gap-1" role="group">
          <Button
            aria-pressed={filter === 'all'}
            className="h-6 px-1.5 text-[0.625rem]"
            data-live-graph-all-filter
            onClick={() => setFilter('all')}
            size="xs"
            type="button"
            variant={filter === 'all' ? 'secondary' : 'ghost'}
          >
            {t.liveGraph.allTasks}
          </Button>
          <Button
            aria-pressed={filter === 'active'}
            className="h-6 px-1.5 text-[0.625rem]"
            data-live-graph-active-count
            onClick={() => setFilter('active')}
            size="xs"
            type="button"
            variant={filter === 'active' ? 'secondary' : 'ghost'}
          >
            <strong className="font-semibold text-(--ui-text-primary)">{activeTasks.length}</strong>{' '}
            {t.liveGraph.activeTasks}
          </Button>
          <Button
            aria-pressed={filter === 'completed'}
            className="h-6 px-1.5 text-[0.625rem]"
            data-live-graph-completed-count
            onClick={() => setFilter('completed')}
            size="xs"
            type="button"
            variant={filter === 'completed' ? 'secondary' : 'ghost'}
          >
            <strong className="font-semibold text-(--ui-text-primary)">{completedTasks.length}</strong>{' '}
            {t.liveGraph.completedTasks}
          </Button>
          <Button
            aria-pressed={filter === 'attention'}
            className="h-6 px-1.5 text-[0.625rem] text-(--ui-yellow)"
            data-live-graph-attention-count
            onClick={() => setFilter('attention')}
            size="xs"
            type="button"
            variant={filter === 'attention' ? 'secondary' : 'ghost'}
          >
            <strong className="font-semibold">{attentionTasks.length}</strong> {t.liveGraph.attentionTasks}
          </Button>
        </div>

        {showAttentionTasks && (attentionTasks.length > 0 || filter === 'attention') && (
          <section
            aria-labelledby={`live-graph-attention-tasks:${workflowScope}`}
            className="grid min-w-0 gap-2"
            data-live-graph-attention-tasks
          >
            <div className="flex items-center justify-between gap-2">
              <h3
                className="m-0 text-[0.625rem] font-semibold tracking-wide text-(--ui-yellow) uppercase"
                id={`live-graph-attention-tasks:${workflowScope}`}
              >
                {t.liveGraph.attentionTasks}
              </h3>
              <span className="font-mono text-[0.625rem] text-(--ui-text-quaternary)">{attentionTasks.length}</span>
            </div>
            {attentionTasks.length === 0 ? (
              <div className="flex items-center gap-2 rounded-lg border border-dashed border-(--ui-stroke-tertiary) px-3 py-3 text-[0.6875rem] text-(--ui-text-tertiary)">
                <Codicon className="text-(--ui-green)" name="pass-filled" />
                {t.liveGraph.noAttentionTasks}
              </div>
            ) : (
              <div className="grid min-w-0 gap-2">
                <AnimatePresence initial={false}>
                  {attentionTasks.map(task => (
                    <TaskCard
                      key={task.id}
                      nowMs={nowMs}
                      onSelect={() => onSelectTask(task.id)}
                      reducedMotion={reducedMotion}
                      task={task}
                    />
                  ))}
                </AnimatePresence>
              </div>
            )}
          </section>
        )}

        {showActiveTasks && (
          <section
            aria-labelledby={`live-graph-active-tasks:${workflowScope}`}
            className="grid min-w-0 gap-2"
            data-live-graph-active-tasks
          >
            <div className="flex items-center justify-between gap-2">
              <h3
                className="m-0 text-[0.625rem] font-semibold tracking-wide text-(--ui-text-tertiary) uppercase"
                id={`live-graph-active-tasks:${workflowScope}`}
              >
                {t.liveGraph.activeTasks}
              </h3>
              <span className="font-mono text-[0.625rem] text-(--ui-text-quaternary)">{activeTasks.length}</span>
            </div>
            {activeTasks.length === 0 ? (
              <div className="flex items-center gap-2 rounded-lg border border-dashed border-(--ui-stroke-tertiary) px-3 py-3 text-[0.6875rem] text-(--ui-text-tertiary)">
                <Codicon className="text-(--ui-green)" name="pass-filled" />
                {t.liveGraph.allTasksCompleted}
              </div>
            ) : (
              <div className="grid min-w-0 gap-2">
                <AnimatePresence initial={false}>
                  {activeTasks.map(task => (
                    <TaskCard
                      key={task.id}
                      nowMs={nowMs}
                      onSelect={() => onSelectTask(task.id)}
                      reducedMotion={reducedMotion}
                      task={task}
                    />
                  ))}
                </AnimatePresence>
              </div>
            )}
          </section>
        )}

        {showCompletedTasks && (completedTasks.length > 0 || filter === 'completed') && (
          <section aria-labelledby={`live-graph-completed-tasks:${workflowScope}`} className="grid min-w-0 gap-1">
            <div className="flex items-center gap-2 py-0.5">
              <h3
                className="m-0 shrink-0 text-[0.625rem] font-semibold tracking-wide text-(--ui-text-tertiary) uppercase"
                id={`live-graph-completed-tasks:${workflowScope}`}
              >
                {t.liveGraph.completedTasks}
              </h3>
              <span className="h-px min-w-0 flex-1 bg-(--ui-stroke-tertiary)" />
              <span className="font-mono text-[0.625rem] text-(--ui-text-quaternary)">{completedTasks.length}</span>
            </div>
            {completedTasks.length === 0 ? (
              <div className="flex items-center gap-2 rounded-lg border border-dashed border-(--ui-stroke-tertiary) px-3 py-3 text-[0.6875rem] text-(--ui-text-tertiary)">
                <Codicon className="text-(--ui-text-quaternary)" name="circle-outline" />
                {t.liveGraph.noCompletedTasks}
              </div>
            ) : (
              <div className="grid min-w-0 gap-0.5">
                <AnimatePresence initial={false}>
                  {visibleCompletedTasks.map(task => (
                    <CompletedTaskRow
                      key={task.id}
                      nowMs={nowMs}
                      onSelect={() => onSelectTask(task.id)}
                      reducedMotion={reducedMotion}
                      task={task}
                    />
                  ))}
                </AnimatePresence>
              </div>
            )}
            {hiddenCompletedCount > 0 && (
              <Button
                className="mt-1 h-7 justify-start px-2 text-[0.625rem]"
                onClick={() => setVisibleCompletedCount(current => current + COMPLETED_PAGE_INCREMENT)}
                size="xs"
                type="button"
                variant="text"
              >
                <Codicon name="chevron-down" />
                {t.liveGraph.showMoreCompleted(hiddenCompletedCount)}
              </Button>
            )}
          </section>
        )}
      </div>
    </LayoutGroup>
  )
})
