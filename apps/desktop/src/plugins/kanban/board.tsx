/**
 * The Kanban board page — mounted at `/kanban` (a ROUTES_AREA contribution) in
 * the workspace pane. The desktop port of the dashboard board: one compact
 * header row (count, filter kebab, search, settings, new task — the board
 * SWITCHER lives in the titlebar, see board-switcher.tsx), columns in
 * BOARD_COLUMNS order, drag-to-move (optimistic, workflow-checked),
 * primary-modifier-click multi-select with a floating bulk bar, right-click
 * actions, and the detail drawer. Dispatch nudges ride every write (see api.ts).
 */

import {
  Button,
  cn,
  Codicon,
  compactNumber,
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
  Contribute,
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  ErrorState,
  formatModifierToken,
  host,
  Input,
  Loader,
  SearchField,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Switch,
  Textarea,
  Tip,
  TITLEBAR_AREAS,
  useGrabScroll,
  useMutation,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import {
  type CSSProperties,
  type DragEvent as ReactDragEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState
} from 'react'

import {
  $boardSlug,
  $collapsedLanes,
  $introDismissed,
  $lanesByProfile,
  boardKey,
  BOARDS_KEY,
  bulkTasks,
  createTask,
  deleteTask,
  estimateNew,
  fetchBoard,
  fetchBoards,
  fetchOrchestration,
  orchestrationKey,
  patchTask,
  profileQueryOptions,
  taskKey
} from './api'
import { BoardSwitcher } from './board-switcher'
import { TaskDrawer } from './drawer'
import { EMPTY_OVERRIDE, ModelOverrideField, overrideCreateFields, type TaskModelOverride } from './model-override'
import { OrchestrationPanel } from './orchestration'
import { columnMeta, type KanbanBoard, type KanbanTask, type TaskEstimate } from './types'
import {
  $newTaskLane,
  ago,
  type ArcState,
  arcState,
  Avatar,
  columnHelp,
  columnLabel,
  errText,
  FIELD_LABEL,
  isLockedTarget,
  lockedReason,
  RunClock,
  shortId,
  useKanban,
  useOrchestration
} from './ui'

// ── optimistic board edits (reconciled by the follow-up refresh) ─────────────

function moveCard(board: KanbanBoard, id: string, toStatus: string): KanbanBoard {
  let moved: KanbanTask | undefined

  const columns = board.columns.map(col => ({
    ...col,
    tasks: col.tasks.filter(task => {
      if (task.id !== id) {
        return true
      }

      moved = { ...task, status: toStatus }

      return false
    })
  }))

  if (!moved) {
    return board
  }

  return {
    ...board,
    columns: columns.map(col => (col.name === toStatus ? { ...col, tasks: [moved!, ...col.tasks] } : col))
  }
}

function removeCard(board: KanbanBoard, id: string): KanbanBoard {
  return { ...board, columns: board.columns.map(col => ({ ...col, tasks: col.tasks.filter(t => t.id !== id) })) }
}

// ── card ─────────────────────────────────────────────────────────────────────

function Meta({ children, icon }: { children: ReactNode; icon: string }) {
  return (
    <span className="inline-flex items-center gap-1">
      <Codicon name={icon} size="0.7rem" />
      {children}
    </span>
  )
}

function CardFooter({
  arc,
  fallback,
  orchestrator,
  task
}: {
  arc: ArcState | null
  fallback: string
  orchestrator: string
  task: KanbanTask
}) {
  const k = useKanban()
  const created = ago(task.created_at)
  const links = task.link_counts ? task.link_counts.parents + task.link_counts.children : 0
  // Ready + no assignee: with a configured default assignee the dispatcher
  // auto-assigns on its next tick (#27145) — say THAT, not "won't run". Only
  // a board with no fallback has the genuine silent failure.
  const unassignedReady = task.status === 'ready' && !task.assignee

  // The agent on the hook for a queued card: the explicit assignee, else the
  // auto-default (ready), else the specifier that rewrites triage cards.
  const attached = task.assignee || (task.status === 'ready' ? fallback : task.status === 'triage' ? orchestrator : '')

  const meta = columnMeta(task.status)

  return (
    <div className="flex items-center gap-2 whitespace-nowrap text-[0.625rem] text-(--ui-text-tertiary)">
      {arc === 'queued' && attached ? (
        // WHO is coming for the card. The arc only animates once the agent is
        // actually working; while queued, the named chip carries "attached".
        <Tip
          label={
            task.status === 'review'
              ? k.reviewChecking
              : task.assignee
                ? k.attachedTip(attached)
                : task.status === 'triage'
                  ? k.orchestratorTip(attached)
                  : k.autoAssignTip(attached)
          }
        >
          <span className="inline-flex min-w-0 cursor-help items-center gap-1 font-medium" style={{ color: meta.tone }}>
            <Avatar name={attached} size="1.125rem" />
            <span className="truncate">
              {!task.assignee && '→ '}
              {attached}
            </span>
          </span>
        </Tip>
      ) : task.assignee ? (
        <Avatar name={task.assignee} size="1.125rem" />
      ) : null}
      {arc === 'running' && (
        <Tip label={k.arcRunning}>
          <span className="shrink-0 cursor-help">
            <RunClock task={task} />
          </span>
        </Tip>
      )}
      {arc === 'stale' && (
        <Tip label={k.arcStale}>
          <span className="shrink-0 cursor-help font-medium text-amber-500">{k.noHeartbeat}</span>
        </Tip>
      )}
      {unassignedReady && !fallback && (
        <Tip label={k.wontRunTip}>
          <span className="inline-flex shrink-0 cursor-help items-center gap-1 text-amber-500">
            <Codicon name="debug-disconnect" size="0.7rem" />
            {k.wontRun}
          </span>
        </Tip>
      )}
      <div className="ml-auto flex min-w-0 shrink items-center gap-2">
        {typeof task.priority === 'number' && task.priority > 0 && (
          <span className="inline-flex items-center gap-0.5 text-amber-500">
            <Codicon name="arrow-up" size="0.7rem" />
            {task.priority}
          </span>
        )}
        {task.progress && task.progress.total > 0 && (
          <Meta icon="checklist">
            {task.progress.done}/{task.progress.total}
          </Meta>
        )}
        {Boolean(task.comment_count) && <Meta icon="comment">{task.comment_count}</Meta>}
        {links > 0 && <Meta icon="references">{links}</Meta>}
        {task.warnings && task.warnings.count > 0 && (
          <span className="inline-flex items-center gap-0.5 text-destructive">
            <Codicon name="warning" size="0.7rem" />
            {task.warnings.count}
          </span>
        )}
        {created && !task.assignee && !unassignedReady ? (
          <span className="text-(--ui-text-quaternary)">{created}</span>
        ) : null}
        <span className="min-w-0 truncate font-mono text-(--ui-text-quaternary)">{shortId(task.id)}</span>
      </div>
    </div>
  )
}

function nestedInteractiveTarget(currentTarget: HTMLElement, target: EventTarget | null): boolean {
  if (!(target instanceof Element)) {
    return false
  }

  const interactive = target.closest(
    'button, a, input, select, textarea, [role="button"], [role="menuitem"], [contenteditable="true"]'
  )

  return interactive !== null && interactive !== currentTarget
}

export function Card({
  columns,
  onDelete,
  onMove,
  onOpen,
  onToggleSelect,
  selected,
  task
}: {
  columns: string[]
  onDelete: (id: string) => void
  onMove: (id: string, status: string) => void
  onOpen: (id: string) => void
  onToggleSelect: (id: string) => void
  selected: boolean
  task: KanbanTask
}) {
  const k = useKanban()
  const [dragging, setDragging] = useState(false)
  const meta = columnMeta(task.status)
  const summary = task.latest_summary || task.body
  const orchestration = useOrchestration()
  const fallback = orchestration?.resolved_default_assignee?.trim() ?? ''
  const orchestrator = orchestration?.resolved_orchestrator_profile ?? ''
  const arc = arcState(task, fallback)

  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <div
          aria-label={task.title || task.id}
          className={cn(
            'group relative flex cursor-grab flex-col gap-2 rounded-md border border-(--ui-stroke-tertiary) border-l-2 bg-(--ui-bg-elevated) p-2.5',
            // Hover matches the provider-picker rows: a quiet primary fill;
            // selected = the theme's focus color (same as a focused input).
            'transition-colors hover:bg-primary/[0.06] active:cursor-grabbing',
            selected && 'border-(--dt-composer-ring) bg-[color-mix(in_srgb,var(--dt-composer-ring)_7%,transparent)]',
            dragging && 'opacity-40'
          )}
          draggable
          onClick={event => {
            if (nestedInteractiveTarget(event.currentTarget, event.target)) {
              return
            }

            if (event.metaKey || event.ctrlKey) {
              onToggleSelect(task.id)
            } else {
              onOpen(task.id)
            }
          }}
          onDragEnd={() => setDragging(false)}
          onDragStart={event => {
            event.dataTransfer.setData('text/plain', task.id)
            event.dataTransfer.effectAllowed = 'move'
            // Snapshot the drag image before dimming the source, so the ghost
            // stays a solid card (dimming first would bake 40% into it).
            event.dataTransfer.setDragImage(event.currentTarget, event.nativeEvent.offsetX, event.nativeEvent.offsetY)
            setDragging(true)
          }}
          onKeyDown={event => {
            if (
              nestedInteractiveTarget(event.currentTarget, event.target) ||
              (event.key !== 'Enter' && event.key !== ' ')
            ) {
              return
            }

            event.preventDefault()

            if (event.metaKey || event.ctrlKey) {
              onToggleSelect(task.id)
            } else {
              onOpen(task.id)
            }
          }}
          role="button"
          style={{ '--kanban-tone': meta.tone, borderLeftColor: meta.tone } as CSSProperties}
          tabIndex={0}
        >
          {/* Machine-activity arc: animates ONLY while an agent is actually on
              the card (claimed + working; amber when the heartbeat is gone).
              Queued attachment is the footer's named-agent chip — a moving
              border on an idle card would lie. Hidden during drag/selection
              so those states stay legible. */}
          {(arc === 'running' || arc === 'stale') && !dragging && !selected && (
            <span aria-hidden className={cn('kanban-arc', arc === 'stale' && 'kanban-arc--stale')} />
          )}
          <span className="line-clamp-2 text-[0.8125rem] font-medium leading-snug text-foreground">
            {task.title || task.id}
          </span>
          {summary && (
            <span className="line-clamp-2 text-[0.6875rem] leading-snug text-(--ui-text-tertiary)">{summary}</span>
          )}
          <CardFooter arc={arc} fallback={fallback} orchestrator={orchestrator} task={task} />
        </div>
      </ContextMenuTrigger>
      <ContextMenuContent>
        <ContextMenuItem onSelect={() => onOpen(task.id)}>
          <Codicon name="link-external" size="0.85rem" />
          {k.open}
        </ContextMenuItem>
        <ContextMenuItem onSelect={() => onToggleSelect(task.id)}>
          <Codicon name={selected ? 'close' : 'check-all'} size="0.85rem" />
          {selected ? k.deselect : k.select(formatModifierToken('mod'))}
        </ContextMenuItem>
        <ContextMenuSeparator />
        {columns
          .filter(name => name !== task.status && !isLockedTarget(name))
          .map(name => (
            <ContextMenuItem key={name} onSelect={() => onMove(task.id, name)}>
              <span className="size-2 rounded-full" style={{ backgroundColor: columnMeta(name).tone }} />
              {k.moveTo(columnLabel(k, name))}
            </ContextMenuItem>
          ))}
        <ContextMenuSeparator />
        <ContextMenuItem onSelect={() => onDelete(task.id)} variant="destructive">
          <Codicon name="trash" size="0.85rem" />
          {k.delete}
        </ContextMenuItem>
      </ContextMenuContent>
    </ContextMenu>
  )
}

// ── column ───────────────────────────────────────────────────────────────────

function Column({
  collapsed,
  column,
  columns,
  onAdd,
  onDelete,
  onDropTask,
  onMove,
  onOpen,
  onToggle,
  onToggleSelect,
  selected
}: {
  collapsed: boolean
  column: { name: string; tasks: KanbanTask[] }
  columns: string[]
  onAdd: (status: string) => void
  onDelete: (id: string) => void
  onDropTask: (id: string, status: string) => void
  onMove: (id: string, status: string) => void
  onOpen: (id: string) => void
  onToggle: () => void
  onToggleSelect: (id: string) => void
  selected: ReadonlySet<string>
}) {
  const k = useKanban()
  const [over, setOver] = useState(false)
  const meta = columnMeta(column.name)
  const label = columnLabel(k, column.name)
  const locked = isLockedTarget(column.name)
  const byProfile = useValue($lanesByProfile)

  // The dashboard's "lanes by profile": sub-group Running by assignee so a
  // fleet's in-flight work reads per-worker. Null = flat (off, or trivial).
  const lanes = useMemo(() => {
    if (!byProfile || column.name !== 'running' || column.tasks.length === 0) {
      return null
    }

    const groups = new Map<string, KanbanTask[]>()

    for (const task of column.tasks) {
      const key = task.assignee || UNASSIGNED_LANE
      groups.set(key, [...(groups.get(key) ?? []), task])
    }

    return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b))
  }, [byProfile, column])

  const dragHandlers = {
    onDragLeave: () => setOver(false),
    onDragOver: (event: ReactDragEvent<HTMLElement>) => {
      // Locked lanes don't preventDefault → the OS shows the no-drop cursor
      // and the drop event never fires. The lane is honest about itself.
      if (locked) {
        event.dataTransfer.dropEffect = 'none'

        return
      }

      event.preventDefault()
      event.dataTransfer.dropEffect = 'move'
      setOver(true)
    },
    onDrop: (event: ReactDragEvent<HTMLElement>) => {
      event.preventDefault()
      setOver(false)
      const id = event.dataTransfer.getData('text/plain')

      if (id) {
        onDropTask(id, column.name)
      }
    }
  }

  const wash = over && !locked ? 'bg-(--ui-bg-quinary)' : 'bg-[color-mix(in_srgb,var(--ui-bg-quinary)_50%,transparent)]'

  // Collapsed = a thin vertical rail: dot, sideways label, count. Still a live
  // drop target (drop straight onto the rail); click expands. The dot sits in
  // the same h-5 header row as an expanded lane's, so dots align across the
  // board regardless of collapse state.
  if (collapsed) {
    return (
      <button
        {...dragHandlers}
        aria-label={k.expand(label)}
        className={cn(
          'flex h-full w-8 shrink-0 flex-col items-center gap-1.5 rounded-lg p-2 transition-colors hover:bg-(--ui-bg-quinary)',
          wash
        )}
        onClick={onToggle}
        type="button"
      >
        <span className="grid h-5 shrink-0 place-items-center">
          <span className="size-1.5 rounded-full" style={{ backgroundColor: meta.tone }} />
        </span>
        <span className="text-[0.6875rem] font-medium uppercase tracking-wide text-(--ui-text-tertiary) [writing-mode:vertical-rl]">
          {label}
        </span>
        {column.tasks.length > 0 && (
          <span className="text-[0.625rem] tabular-nums text-(--ui-text-quaternary)">{column.tasks.length}</span>
        )}
      </button>
    )
  }

  return (
    <div
      {...dragHandlers}
      className={cn('group/col flex h-full w-64 shrink-0 flex-col rounded-lg p-2 transition-colors', wash)}
    >
      <header className="mb-1.5 flex h-5 items-center gap-1.5 px-1">
        <span className="size-1.5 rounded-full" style={{ backgroundColor: meta.tone }} />
        <Tip label={columnHelp(k, column.name)}>
          <span className="cursor-help text-[0.6875rem] font-medium uppercase tracking-wide text-(--ui-text-tertiary)">
            {label}
          </span>
        </Tip>
        <span className="text-[0.625rem] tabular-nums text-(--ui-text-quaternary)">{column.tasks.length}</span>
        <button
          aria-label={k.collapse(label)}
          className="ml-auto grid size-5 place-items-center rounded text-(--ui-text-tertiary) opacity-0 transition-opacity hover:bg-(--chrome-action-hover) hover:text-foreground focus-visible:opacity-100 group-hover/col:opacity-100"
          onClick={onToggle}
          type="button"
        >
          <Codicon name="chevron-left" size="0.75rem" />
        </button>
      </header>
      <div className="relative flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto">
        {lanes
          ? lanes.map(([assignee, tasks]) => (
              <div className="flex flex-col gap-2" key={assignee}>
                <div className="flex items-center gap-1.5 px-1 pt-1 text-[0.625rem] text-(--ui-text-quaternary)">
                  {assignee !== UNASSIGNED_LANE && <Avatar name={assignee} size="0.875rem" />}
                  {assignee === UNASSIGNED_LANE ? k.unassigned : assignee}
                  <span className="tabular-nums">{tasks.length}</span>
                </div>
                {tasks.map(task => (
                  <Card
                    columns={columns}
                    key={task.id}
                    onDelete={onDelete}
                    onMove={onMove}
                    onOpen={onOpen}
                    onToggleSelect={onToggleSelect}
                    selected={selected.has(task.id)}
                    task={task}
                  />
                ))}
              </div>
            ))
          : column.tasks.map(task => (
              <Card
                columns={columns}
                key={task.id}
                onDelete={onDelete}
                onMove={onMove}
                onOpen={onOpen}
                onToggleSelect={onToggleSelect}
                selected={selected.has(task.id)}
                task={task}
              />
            ))}
        {/* Jira-style lane add — dashed, faded in on lane hover. Opacity (not
            display) so it always holds its slot and never thrashes layout.
            Locked lanes get none: you can't create into a system state. */}
        {!locked && (
          <button
            aria-label={k.newTaskIn(label)}
            className="flex shrink-0 items-center justify-center rounded-md border border-dashed border-(--ui-stroke-secondary) py-1.5 text-(--ui-text-tertiary) opacity-0 transition-[opacity,color,border-color] group-hover/col:opacity-100 hover:border-(--ui-text-quaternary) hover:bg-(--chrome-action-hover) hover:text-foreground focus-visible:opacity-100"
            onClick={() => onAdd(column.name)}
            type="button"
          >
            <Codicon name="add" size="0.8rem" />
          </button>
        )}
        {column.tasks.length === 0 && (
          <div className="pointer-events-none absolute inset-0 grid place-items-center text-[0.6875rem] text-(--ui-text-quaternary)">
            {k.empty}
          </div>
        )}
      </div>
    </div>
  )
}

// ── dialogs ──────────────────────────────────────────────────────────────────

const NO_PARENT = '__none__'
const PARKED = '__parked__'
const WORKSPACE_KINDS = ['scratch', 'worktree', 'dir'] as const

interface NewTaskOperation {
  board: string
  generation: number
  target: string
}

export function resolveNewTaskAssignee(
  selection: string,
  effectiveDefault: null | string | undefined,
  effectiveProfiles: ReadonlySet<string>
): string | undefined {
  if (selection === PARKED) {
    return undefined
  }

  if (selection && effectiveProfiles.has(selection)) {
    return selection
  }

  return effectiveDefault && effectiveProfiles.has(effectiveDefault) ? effectiveDefault : undefined
}

function Field({ children, label }: { children: ReactNode; label: string }) {
  return (
    <label className="flex flex-col gap-1">
      <span className={FIELD_LABEL}>{label}</span>
      {children}
    </label>
  )
}

export function NewTaskDialog({
  onClose,
  parents,
  target
}: {
  onClose: (board: string) => void
  parents: Array<{ id: string; title: string }>
  target: null | string
}) {
  const k = useKanban()
  const qc = useQueryClient()
  const selectedSlug = useValue($boardSlug)
  const dialogOpen = Boolean(target)
  const lifecycle = useRef({ board: selectedSlug, generation: 0, open: dialogOpen, target })
  const createOwner = useRef<null | number>(null)
  const estimateOwner = useRef<null | number>(null)

  if (
    lifecycle.current.board !== selectedSlug ||
    lifecycle.current.open !== dialogOpen ||
    lifecycle.current.target !== target
  ) {
    lifecycle.current = {
      board: selectedSlug,
      generation: lifecycle.current.generation + 1,
      open: dialogOpen,
      target
    }
    createOwner.current = null
    estimateOwner.current = null
  }

  const lifecycleGeneration = lifecycle.current.generation

  const operation: NewTaskOperation | null = target
    ? { board: selectedSlug, generation: lifecycleGeneration, target }
    : null

  const isCurrent = (candidate: NewTaskOperation) => {
    const current = lifecycle.current

    return (
      current.open &&
      current.board === candidate.board &&
      current.generation === candidate.generation &&
      current.target === candidate.target
    )
  }

  const rosterQuery = useQuery(profileQueryOptions(selectedSlug))

  const orchestrationQuery = useQuery({
    queryFn: () => fetchOrchestration(selectedSlug),
    queryKey: orchestrationKey(selectedSlug),
    staleTime: 60_000
  })

  const roster = rosterQuery.data
  const orchestration = orchestrationQuery.data
  const policyError = rosterQuery.error ?? orchestrationQuery.error

  const effectiveProfiles = new Set(
    (roster?.profiles ?? [])
      .filter(profile => profile.effective_allowed && orchestration?.effective_allowed_profiles.includes(profile.name))
      .map(profile => profile.name)
  )

  // Title-only creates run under the effective orchestration default when one
  // exists. A deny-all board has no default and therefore parks the task; never
  // fabricate a profile that the board policy excludes.
  const candidateDefault = orchestration?.resolved_default_assignee ?? ''
  const resolvedDefault = effectiveProfiles.has(candidateDefault) ? candidateDefault : ''

  const policyReady =
    Boolean(roster && orchestration) && !policyError && !rosterQuery.isFetching && !orchestrationQuery.isFetching

  // Board-level workspace default: a task inherits the current board's
  // configured project dir (scratch when unset, worktree in a git repo, else
  // dir) unless the operator overrides it below. Set the board default in the
  // board switcher's "Board settings…".
  const { data: boards } = useQuery({ queryKey: BOARDS_KEY, queryFn: fetchBoards, staleTime: 30_000 })
  const currentBoard = boards?.boards.find(b => b.slug === (selectedSlug || boards.current))
  const boardDefaultKind = currentBoard?.default_workspace_kind || 'scratch'
  const boardDefaultDir = currentBoard?.default_workdir || ''

  const isTriage = target === 'triage'
  const [title, setTitle] = useState('')
  const [bodyText, setBodyText] = useState('')
  const [assignee, setAssignee] = useState('')
  const [priority, setPriority] = useState('0')
  const [skills, setSkills] = useState('')
  const [workspaceKind, setWorkspaceKind] = useState<string>(boardDefaultKind)
  // Empty = inherit the board's default project dir (backend resolves it);
  // a path here overrides just this task. Only meaningful for dir/worktree.
  const [workspacePath, setWorkspacePath] = useState('')
  const [parent, setParent] = useState('')
  const [modelOverride, setModelOverride] = useState<TaskModelOverride>(EMPTY_OVERRIDE)
  const [goalMode, setGoalMode] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<null | string>(null)
  const [estimate, setEstimate] = useState<null | TaskEstimate>(null)
  const [estimating, setEstimating] = useState(false)
  const [workspaceTouched, setWorkspaceTouched] = useState(false)

  // Reset per open/board only. Metadata can arrive after the dialog opens, but
  // it must never erase title/body input the user already typed.
  useEffect(() => {
    const current = lifecycle.current

    if (
      !target ||
      !current.open ||
      current.board !== selectedSlug ||
      current.generation !== lifecycleGeneration ||
      current.target !== target
    ) {
      return
    }

    setTitle('')
    setBodyText('')
    setAssignee('')
    setPriority('0')
    setSkills('')
    setWorkspaceTouched(false)
    setWorkspaceKind('scratch')
    setWorkspacePath('')
    setParent('')
    setModelOverride(EMPTY_OVERRIDE)
    setGoalMode(false)
    setError(null)
    setBusy(false)
    setEstimate(null)
    setEstimating(false)
  }, [lifecycleGeneration, selectedSlug, target])

  // Adopt a late board workspace default only while that selector is pristine.
  useEffect(() => {
    const current = lifecycle.current

    if (
      target &&
      !workspaceTouched &&
      current.open &&
      current.board === selectedSlug &&
      current.generation === lifecycleGeneration &&
      current.target === target
    ) {
      setWorkspaceKind(boardDefaultKind)
    }
  }, [boardDefaultKind, lifecycleGeneration, selectedSlug, target, workspaceTouched])

  const closeDialog = (candidate: NewTaskOperation | null) => {
    if (!candidate || !isCurrent(candidate)) {
      return
    }

    const current = lifecycle.current
    lifecycle.current = {
      board: current.board,
      generation: current.generation + 1,
      open: false,
      target: null
    }
    onClose(candidate.board)
  }

  // Rough effort estimate from the typed title/body (before the task exists),
  // via the auto-routed auxiliary model. Makes a model call — explicit action.
  const runEstimate = async () => {
    if (!operation || !title.trim() || estimateOwner.current === operation.generation) {
      return
    }

    const candidate = operation
    const estimateTitle = title.trim()
    const estimateBody = bodyText.trim()
    estimateOwner.current = candidate.generation
    setEstimating(true)

    try {
      const result = await estimateNew(estimateTitle, estimateBody)

      if (!isCurrent(candidate)) {
        return
      }

      if (result.ok) {
        setEstimate(result)
      } else {
        host.notify({ kind: 'warning', message: result.reason || k.couldNotEstimate })
      }
    } catch (err) {
      if (isCurrent(candidate)) {
        host.notify({ kind: 'error', message: errText(err) })
      }
    } finally {
      if (isCurrent(candidate) && estimateOwner.current === candidate.generation) {
        estimateOwner.current = null
        setEstimating(false)
      }
    }
  }

  const submit = async () => {
    const trimmed = title.trim()

    if (!trimmed || !operation || createOwner.current === operation.generation || !policyReady) {
      return
    }

    const candidate = operation
    const board = candidate.board
    createOwner.current = candidate.generation
    setBusy(true)
    setError(null)

    try {
      const skillList = skills
        .split(',')
        .map(s => s.trim())
        .filter(Boolean)

      // create() derives status (triage flag → 'triage', else 'ready'); move to
      // the requested column when they differ, so a per-column add lands right.
      const { task, warning } = await createTask(board, {
        assignee: resolveNewTaskAssignee(assignee, resolvedDefault, effectiveProfiles),
        body: bodyText.trim() || undefined,
        goal_mode: goalMode,
        parents: parent ? [parent] : undefined,
        priority: Number(priority) || 0,
        skills: skillList.length ? skillList : undefined,
        title: trimmed,
        triage: candidate.target === 'triage',
        workspace_kind: workspaceKind,
        ...overrideCreateFields(modelOverride),
        // Empty → backend inherits the board's default project dir.
        workspace_path: workspaceKind !== 'scratch' && workspacePath.trim() ? workspacePath.trim() : undefined
      })

      if (task && task.status !== candidate.target) {
        await patchTask(board, task.id, { status: candidate.target })
      }

      // Dispatcher-presence warning ("this ready task will sit idle") — not an
      // error, but the user should know.
      if (warning && isCurrent(candidate)) {
        host.notify({ kind: 'warning', message: warning })
      }

      await Promise.all([
        qc.invalidateQueries({ queryKey: ['kanban', 'board', board] }),
        qc.invalidateQueries({ queryKey: BOARDS_KEY }),
        ...(task ? [qc.invalidateQueries({ queryKey: taskKey(board, task.id) })] : [])
      ])

      if (isCurrent(candidate)) {
        closeDialog(candidate)
      }
    } catch (err) {
      if (isCurrent(candidate)) {
        setError(errText(err))
      }
    } finally {
      if (isCurrent(candidate) && createOwner.current === candidate.generation) {
        createOwner.current = null
        setBusy(false)
      }
    }
  }

  return (
    <Dialog onOpenChange={open => !open && closeDialog(operation)} open={dialogOpen}>
      {/* `overflow-visible`: DialogContent publishes ITSELF as the portal
          container for popovers opened inside it (dialog-portal-context), and
          its default `overflow-y-auto` then crops them at the dialog's edge —
          the model menu below is born inside that scroll box. This dialog
          already owns a scroller on its body div, so the shell's clip is
          redundant here and dropping it is safe. The general fix to
          DialogContent is in flight as #75600; when that lands this override
          becomes a no-op and can go. */}
      <DialogContent className="w-[min(42rem,94vw)] max-w-none overflow-visible">
        <DialogHeader>
          <DialogTitle>{target ? k.newTaskIn(columnLabel(k, target)) : k.newTask}</DialogTitle>
        </DialogHeader>
        <div className="flex max-h-[min(72vh,44rem)] flex-col gap-3 overflow-y-auto pr-0.5">
          <Input
            autoFocus
            onChange={event => setTitle(event.target.value)}
            onKeyDown={event => {
              if (event.key === 'Enter') {
                event.preventDefault()
                void submit()
              }
            }}
            placeholder={isTriage ? k.titlePlaceholderTriage : k.titlePlaceholder}
            value={title}
          />
          <Textarea
            className="min-h-20"
            onChange={event => setBodyText(event.target.value)}
            placeholder={k.descPlaceholder}
            value={bodyText}
          />

          <div className="grid grid-cols-2 gap-3">
            <Field label={k.priority}>
              <Input onChange={event => setPriority(event.target.value)} type="number" value={priority} />
            </Field>
            <Field label={k.workspace}>
              <Select
                onValueChange={value => {
                  setWorkspaceTouched(true)
                  setWorkspaceKind(value)
                }}
                value={workspaceKind}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {WORKSPACE_KINDS.map(kind => (
                    <SelectItem key={kind} value={kind}>
                      {kind}
                      {kind === boardDefaultKind ? k.boardDefaultSuffix : ''}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
          </div>

          {workspaceKind !== 'scratch' && (
            <Field label={k.workspaceOverride}>
              <Input
                onChange={event => setWorkspacePath(event.target.value)}
                placeholder={boardDefaultDir || k.workspaceInherit}
                value={workspacePath}
              />
              <span className="text-[0.625rem] text-(--ui-text-quaternary)">
                {boardDefaultDir ? k.workspaceInheritDir(boardDefaultDir) : k.workspaceInheritGeneric}
              </span>
            </Field>
          )}

          {policyError && (
            <ErrorState description={errText(policyError)} title={k.taskPolicyLoadError}>
              <Button
                disabled={rosterQuery.isFetching || orchestrationQuery.isFetching}
                onClick={() => void Promise.all([rosterQuery.refetch(), orchestrationQuery.refetch()])}
                size="sm"
                variant="outline"
              >
                <Codicon name="refresh" size="0.8rem" />
                {k.retry}
              </Button>
            </ErrorState>
          )}

          <Field label={k.assignee}>
            <Select
              disabled={!policyReady}
              onValueChange={v => setAssignee(v === NO_PARENT ? '' : v)}
              value={
                (assignee === PARKED || effectiveProfiles.has(assignee) ? assignee : '') ||
                (resolvedDefault ? NO_PARENT : PARKED)
              }
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {resolvedDefault && <SelectItem value={NO_PARENT}>{k.defaultOption(resolvedDefault)}</SelectItem>}
                {(roster?.profiles ?? [])
                  .filter(profile => profile.effective_allowed && profile.name !== resolvedDefault)
                  .map(profile => (
                    <SelectItem key={profile.name} value={profile.name}>
                      {profile.name}
                    </SelectItem>
                  ))}
                <SelectItem value={PARKED}>{k.parkedOption}</SelectItem>
              </SelectContent>
            </Select>
          </Field>

          <Field label={k.skills}>
            <Input onChange={event => setSkills(event.target.value)} placeholder={k.skillsPlaceholder} value={skills} />
          </Field>

          <Field label={k.model}>
            <ModelOverrideField onChange={setModelOverride} value={modelOverride} />
            <span className="text-[0.625rem] text-(--ui-text-quaternary)">{k.modelHint}</span>
          </Field>

          {parents.length > 0 && (
            <Field label={k.parent}>
              <Select onValueChange={v => setParent(v === NO_PARENT ? '' : v)} value={parent || NO_PARENT}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NO_PARENT}>{k.noParent}</SelectItem>
                  {parents.map(option => (
                    <SelectItem key={option.id} value={option.id}>
                      {option.title || option.id}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
          )}

          <label className="flex cursor-pointer items-center gap-2 text-[0.75rem] text-(--ui-text-secondary)">
            <Switch aria-label={k.goalMode} checked={goalMode} onCheckedChange={setGoalMode} size="xs" />
            {k.goalMode}
          </label>

          {error && <span className="text-[0.75rem] text-destructive">{error}</span>}
        </div>
        <DialogFooter>
          <div className="mr-auto flex items-center gap-1 text-[0.75rem] text-(--ui-text-tertiary)">
            {estimate?.ok ? (
              <>
                <Tip label={estimate.rationale || k.roughEstimate}>
                  <span className="font-medium tabular-nums text-(--ui-text-secondary)">
                    ~{compactNumber(estimate.est_tokens)} {k.tokUnit}
                    {estimate.complexity ? ` · ${k.complexity[estimate.complexity] ?? estimate.complexity}` : ''}
                  </span>
                </Tip>
                <Tip label={k.reEstimate}>
                  <Button
                    aria-label={k.reEstimate}
                    disabled={!title.trim() || estimating}
                    onClick={() => void runEstimate()}
                    size="icon-xs"
                    variant="ghost"
                  >
                    <Codicon name="refresh" size="0.7rem" spinning={estimating} />
                  </Button>
                </Tip>
              </>
            ) : (
              <Tip label={k.estimateTip}>
                <Button
                  disabled={!title.trim() || estimating}
                  onClick={() => void runEstimate()}
                  size="xs"
                  variant="ghost"
                >
                  <Codicon name={estimating ? 'loading' : 'dashboard'} size="0.75rem" spinning={estimating} />
                  {estimating ? k.estimating : k.estimate}
                </Button>
              </Tip>
            )}
          </div>
          <Button onClick={() => closeDialog(operation)} variant="text">
            {k.cancel}
          </Button>
          <Button disabled={!title.trim() || busy || !policyReady} onClick={() => void submit()}>
            {busy ? k.creating : k.createTask}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ── intro ────────────────────────────────────────────────────────────────────

// One-time explainer for the board's core gotcha: this is a dispatcher queue,
// not a todo list. Dismissal persists via plugin storage.
function Intro() {
  const k = useKanban()
  const dismissed = useValue($introDismissed)

  if (dismissed) {
    return null
  }

  return (
    <div
      className="mx-4 mb-2 flex flex-col items-start gap-1.5 rounded-lg bg-(--ui-bg-quinary) px-3 py-2.5 text-[0.75rem] leading-relaxed text-(--ui-text-secondary)"
      data-selectable-text="true"
    >
      <p className="min-w-0">{k.introBody}</p>
      <Button onClick={() => $introDismissed.set(true)} size="inline" variant="textStrong">
        {k.introGotIt}
      </Button>
    </div>
  )
}

const UNASSIGNED_LANE = 'unassigned'

// ── filter kebab ─────────────────────────────────────────────────────────────

function FilterMenu({
  archived,
  assignee,
  board,
  onArchived,
  onAssignee,
  onTenant,
  tenant
}: {
  archived: boolean
  assignee: string
  board: KanbanBoard
  onArchived: (v: boolean) => void
  onAssignee: (v: string) => void
  onTenant: (v: string) => void
  tenant: string
}) {
  const k = useKanban()
  const active = Boolean(assignee || tenant || archived)
  const lanesByProfile = useValue($lanesByProfile)

  const check = (on: boolean) => (on ? <Codicon className="ml-auto" name="check" size="0.8rem" /> : null)

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          aria-label={k.filters}
          className={cn(active && 'bg-(--ui-control-active-background) text-foreground')}
          size="icon-xs"
          variant="ghost"
        >
          <Codicon name="filter" size="0.85rem" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start">
        <DropdownMenuItem onSelect={() => onAssignee('')}>
          {k.allProfiles}
          {check(!assignee)}
        </DropdownMenuItem>
        {board.assignees.map(name => (
          <DropdownMenuItem key={name} onSelect={() => onAssignee(name)}>
            <Avatar name={name} size="0.875rem" />
            {name}
            {check(assignee === name)}
          </DropdownMenuItem>
        ))}
        {board.tenants.length > 0 && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => onTenant('')}>
              {k.allTenants}
              {check(!tenant)}
            </DropdownMenuItem>
            {board.tenants.map(name => (
              <DropdownMenuItem key={name} onSelect={() => onTenant(name)}>
                {name}
                {check(tenant === name)}
              </DropdownMenuItem>
            ))}
          </>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => onArchived(!archived)}>
          {k.showArchived}
          {check(archived)}
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => $lanesByProfile.set(!lanesByProfile)}>
          {k.groupRunning}
          {check(lanesByProfile)}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

// ── selection bar ────────────────────────────────────────────────────────────

/**
 * Floating bulk-actions bar, shown while cards are ⌘-selected. Deliberately
 * leaner than the dashboard's always-on toolbar: move / assign / archive /
 * delete cover the real fleet chores (requeue a batch, archive a sweep of
 * done, reassign after a profile change) via POST /tasks/bulk, which applies
 * per-id and reports partial failures — failed cards stay selected.
 */
interface BulkOperation {
  board: string
  generation: number
  ids: string[]
  owner: symbol
}

export function SelectionBar({
  columns,
  generation,
  isCurrent,
  onClear,
  onDone,
  selected
}: {
  columns: string[]
  generation: number
  isCurrent: (board: string, generation: number) => boolean
  onClear: () => void
  onDone: (board: string, generation: number, failed: string[]) => void
  selected: ReadonlySet<string>
}) {
  const k = useKanban()
  const qc = useQueryClient()
  const slug = useValue($boardSlug)
  const { data: roster } = useQuery(profileQueryOptions(slug))
  const pendingOwner = useRef<null | symbol>(null)
  const [pending, setPending] = useState<null | { board: string; generation: number; owner: symbol }>(null)

  const finish = (
    operation: BulkOperation,
    failed: Array<{ error?: string; id: string }>,
    total: number,
    countChanged: boolean
  ) => {
    const { board, generation: operationGeneration, ids } = operation
    void qc.invalidateQueries({ queryKey: ['kanban', 'board', board] })

    for (const id of ids) {
      void qc.invalidateQueries({ queryKey: taskKey(board, id) })
    }

    if (countChanged) {
      void qc.invalidateQueries({ queryKey: BOARDS_KEY })
    }

    if (!isCurrent(board, operationGeneration)) {
      return
    }

    if (failed.length > 0) {
      host.notify({
        kind: 'warning',
        message: k.bulkFailed(failed.length, total, failed[0].error ?? k.refused)
      })
    }

    onDone(
      board,
      operationGeneration,
      failed.map(f => f.id)
    )
  }

  const bulk = useMutation({
    mutationFn: ({ board, ids, patch }: BulkOperation & { patch: Record<string, unknown> }) =>
      bulkTasks(board, ids, patch),
    onError: (err, vars) => {
      if (isCurrent(vars.board, vars.generation)) {
        host.notify({ kind: 'error', message: errText(err) })
      }
    },
    onSettled: (_data, _error, vars) => {
      if (pendingOwner.current === vars.owner) {
        pendingOwner.current = null

        if (isCurrent(vars.board, vars.generation)) {
          setPending(null)
        }
      }
    },
    onSuccess: (data, vars) =>
      finish(
        vars,
        data.results.filter(r => !r.ok),
        vars.ids.length,
        Object.hasOwn(vars.patch, 'status') || vars.patch.archive === true
      )
  })

  const start = (run: (operation: BulkOperation) => void) => {
    if (pending?.board === slug && pending.generation === generation) {
      return
    }

    const owner = Symbol('kanban-bulk-write')
    const operation = { board: slug, generation, ids: [...selected], owner }
    pendingOwner.current = owner
    setPending({ board: slug, generation, owner })
    run(operation)
  }

  const runBulk = (patch: Record<string, unknown>) => start(operation => bulk.mutate({ ...operation, patch }))

  // No bulk-delete on the backend — fan out per id, same partial-failure story.
  const bulkDelete = useMutation({
    mutationFn: async ({ board, ids }: BulkOperation) => {
      const settled = await Promise.allSettled(ids.map(id => deleteTask(board, id)))

      return ids.flatMap((id, i) => {
        const result = settled[i]

        return result.status === 'rejected' ? [{ error: errText(result.reason), id }] : []
      })
    },
    onError: (err, vars) => {
      if (isCurrent(vars.board, vars.generation)) {
        host.notify({ kind: 'error', message: errText(err) })
      }
    },
    onSettled: (_data, _error, vars) => {
      if (pendingOwner.current === vars.owner) {
        pendingOwner.current = null

        if (isCurrent(vars.board, vars.generation)) {
          setPending(null)
        }
      }
    },
    onSuccess: (failed, vars) => finish(vars, failed, vars.ids.length, true)
  })

  const busy = pending?.board === slug && pending.generation === generation
  // One menu at a time — controlled, so a click on the second trigger can
  // never race Radix's dismiss layer into two open menus.
  const [menu, setMenu] = useState<'assign' | 'move' | null>(null)

  return (
    <div className="pointer-events-none absolute inset-x-0 bottom-4 z-10 flex justify-center px-4">
      {/* Flat overlay: stroke + elevated surface do the separating, no shadow. */}
      <div className="pointer-events-auto flex items-center gap-1 rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-elevated) py-1 pr-1 pl-3">
        <span className="mr-1 text-xs tabular-nums text-(--ui-text-secondary)">{k.nSelected(selected.size)}</span>

        <DropdownMenu onOpenChange={open => setMenu(open ? 'move' : null)} open={menu === 'move'}>
          <DropdownMenuTrigger asChild>
            <Button disabled={busy} size="xs" variant="ghost">
              {k.moveToShort}
              <Codicon name="chevron-down" size="0.7rem" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="center">
            {columns
              .filter(name => !isLockedTarget(name))
              .map(name => (
                <DropdownMenuItem key={name} onSelect={() => runBulk({ status: name })}>
                  <span className="size-2 rounded-full" style={{ backgroundColor: columnMeta(name).tone }} />
                  {columnLabel(k, name)}
                </DropdownMenuItem>
              ))}
          </DropdownMenuContent>
        </DropdownMenu>

        <DropdownMenu onOpenChange={open => setMenu(open ? 'assign' : null)} open={menu === 'assign'}>
          <DropdownMenuTrigger asChild>
            <Button disabled={busy} size="xs" variant="ghost">
              {k.assign}
              <Codicon name="chevron-down" size="0.7rem" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="center">
            {(roster?.profiles ?? [])
              .filter(profile => profile.effective_allowed)
              .map(profile => (
                <DropdownMenuItem
                  key={profile.name}
                  onSelect={() => runBulk({ assignee: profile.name, reclaim_first: true })}
                >
                  <Avatar name={profile.name} size="0.875rem" />
                  {profile.name}
                </DropdownMenuItem>
              ))}
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => runBulk({ assignee: '', reclaim_first: true })}>
              {k.unassignAction}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>

        <Button disabled={busy} onClick={() => runBulk({ archive: true })} size="xs" variant="ghost">
          {k.archive}
        </Button>
        <Button
          className="text-destructive"
          disabled={busy}
          onClick={() => start(operation => bulkDelete.mutate(operation))}
          size="xs"
          variant="ghost"
        >
          {k.delete}
        </Button>

        <Tip label={k.clearSelection}>
          <Button aria-label={k.clearSelection} onClick={onClear} size="icon-xs" variant="ghost">
            <Codicon name="close" size="0.8rem" />
          </Button>
        </Tip>
      </div>
    </div>
  )
}

// ── page ─────────────────────────────────────────────────────────────────────

export function KanbanBoardPage() {
  const k = useKanban()
  const qc = useQueryClient()
  const slug = useValue($boardSlug)
  const [archived, setArchived] = useState(false)

  // Live updates ride the events socket (bindApi); this interval is only the
  // slow heartbeat for socketless paths (OAuth remotes, dropped connections).
  const boardQuery = useQuery({
    queryFn: () => fetchBoard(slug, archived),
    queryKey: boardKey(slug, archived),
    refetchInterval: 60_000
  })

  const { data: board, error } = boardQuery

  const [openId, setOpenId] = useState<null | string>(null)
  const [addStatus, setAddStatus] = useState<null | string>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [tenant, setTenant] = useState('')
  const [assignee, setAssignee] = useState('')
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set())
  const selectedRef = useRef(selected)
  selectedRef.current = selected
  const selectionGeneration = useRef(0)
  const selectionBoard = useRef(slug)
  const currentSlug = useRef(slug)
  currentSlug.current = slug

  const replaceSelection = useCallback((next: ReadonlySet<string>) => {
    selectionGeneration.current += 1
    selectedRef.current = next
    setSelected(next)
  }, [])

  if (selectionBoard.current !== slug) {
    selectionBoard.current = slug
    selectionGeneration.current += 1
  }

  // Observe every board transition, not only the final render, so A→B→A can
  // never make an old bulk completion look current by string equality alone.
  useEffect(
    () =>
      $boardSlug.listen(next => {
        if (selectionBoard.current !== next) {
          selectionBoard.current = next
          selectionGeneration.current += 1
        }
      }),
    []
  )

  useEffect(() => {
    setOpenId(null)
    setAddStatus(null)
    setSettingsOpen(false)
    replaceSelection(new Set())
  }, [replaceSelection, slug])

  // A new-task request raised from outside the page (⌘⌥N, the palette row).
  // The command navigates here and parks the lane; the page picks it up on
  // arrival — whether it was already mounted or is mounting for the first
  // time — then clears it so a later remount can't reopen the dialog.
  const requestedLane = useValue($newTaskLane)

  useEffect(() => {
    if (requestedLane === null) {
      return
    }

    setAddStatus(requestedLane)
    $newTaskLane.set(null)
  }, [requestedLane])

  const toggleSelect = (id: string) => {
    const next = new Set(selectedRef.current)

    if (!next.delete(id)) {
      next.add(id)
    }

    replaceSelection(next)
  }

  // Prune ids that left the board (completed elsewhere, deleted, filtered by
  // a board switch) so the bar's count never lies about what a bulk op hits.
  useEffect(() => {
    if (!board) {
      return
    }

    const alive = new Set(board.columns.flatMap(col => col.tasks.map(task => task.id)))

    const previous = selectedRef.current
    const kept = [...previous].filter(id => alive.has(id))

    if (kept.length !== previous.size) {
      replaceSelection(new Set(kept))
    }
  }, [board, replaceSelection])

  useEffect(() => {
    if (selected.size === 0) {
      return
    }

    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        replaceSelection(new Set())
      }
    }

    window.addEventListener('keydown', onKey)

    return () => window.removeEventListener('keydown', onKey)
  }, [replaceSelection, selected.size])

  const columnNames = board?.columns.map(col => col.name) ?? []

  const parentOptions = useMemo(
    () => board?.columns.flatMap(col => col.tasks).map(task => ({ id: task.id, title: task.title })) ?? [],
    [board]
  )

  // Client-side filters, mirroring the dashboard (search over title/body/id).
  const filtered = useMemo(() => {
    if (!board) {
      return null
    }

    const q = search.trim().toLowerCase()

    const keep = (task: KanbanTask) =>
      (!q || `${task.title} ${task.body ?? ''} ${task.id}`.toLowerCase().includes(q)) &&
      (!tenant || task.tenant === tenant) &&
      (!assignee || task.assignee === assignee)

    return { ...board, columns: board.columns.map(col => ({ ...col, tasks: col.tasks.filter(keep) })) }
  }, [board, search, tenant, assignee])

  const total = filtered?.columns.reduce((sum, col) => sum + col.tasks.length, 0) ?? 0

  const moveMut = useMutation({
    mutationFn: ({
      board: targetBoard,
      id,
      status
    }: {
      archived: boolean
      board: string
      fromStatus: string
      id: string
      status: string
    }) => patchTask(targetBoard, id, { status }),
    onMutate: async ({ archived: targetArchived, board: targetBoard, id, status }) => {
      const key = boardKey(targetBoard, targetArchived)
      await qc.cancelQueries({ queryKey: key })
      const previous = qc.getQueryData<KanbanBoard>(key)

      if (previous) {
        qc.setQueryData(key, moveCard(previous, id, status))
      }

      return { key, previous }
    },
    onError: (err, _vars, context) => {
      if (context?.previous) {
        qc.setQueryData(context.key, context.previous)
      }

      host.notify({ kind: 'error', message: errText(err) })
    },
    onSettled: (_data, _err, vars) => {
      void qc.invalidateQueries({ queryKey: ['kanban', 'board', vars.board] })
      void qc.invalidateQueries({ queryKey: taskKey(vars.board, vars.id) })

      if (vars.fromStatus === 'archived' || vars.status === 'archived') {
        void qc.invalidateQueries({ queryKey: BOARDS_KEY })
      }
    }
  })

  const deleteMut = useMutation({
    mutationFn: ({ board: targetBoard, id }: { archived: boolean; board: string; id: string }) =>
      deleteTask(targetBoard, id),
    onMutate: async ({ archived: targetArchived, board: targetBoard, id }) => {
      const key = boardKey(targetBoard, targetArchived)
      await qc.cancelQueries({ queryKey: key })
      const previous = qc.getQueryData<KanbanBoard>(key)

      if (previous) {
        qc.setQueryData(key, removeCard(previous, id))
      }

      return { key, previous }
    },
    onError: (err, _id, context) => {
      if (context?.previous) {
        qc.setQueryData(context.key, context.previous)
      }

      host.notify({ kind: 'error', message: errText(err) })
    },
    onSettled: (_data, _err, vars) => {
      void qc.invalidateQueries({ queryKey: ['kanban', 'board', vars.board] })
      void qc.invalidateQueries({ queryKey: taskKey(vars.board, vars.id) })
      void qc.invalidateQueries({ queryKey: BOARDS_KEY })
    }
  })

  const onMove = (id: string, status: string) => {
    const task = board?.columns.flatMap(col => col.tasks).find(candidate => candidate.id === id)

    if (!task || task.status === status) {
      return
    }

    if (isLockedTarget(status)) {
      host.notify({ kind: 'info', message: lockedReason(k, status) })

      return
    }

    moveMut.mutate({ archived, board: slug, fromStatus: task.status, id, status })
  }

  const errorMessage = error ? errText(error) : null

  // Grab-to-scrub the lane strip (shared primitive, same as the dashboard's pan).
  const lanesRef = useRef<HTMLDivElement>(null)
  const { grabbing, onMouseDown } = useGrabScroll(lanesRef)

  // Lane collapse: auto (empty → rail) unless the user overrode it. The map
  // stores only deviations from auto, so it stays tiny and self-heals. On a
  // board with no work at all, auto is disabled — a wall of rails teaches
  // nothing, so a fresh board shows its full structure instead.
  const laneOverrides = useValue($collapsedLanes)
  const boardHasWork = (board?.columns.reduce((sum, col) => sum + col.tasks.length, 0) ?? 0) > 0

  // An override only lives for the lane's current empty/non-empty phase: when
  // emptiness flips (last card dragged out, first card dropped in) the stale
  // override is dropped and auto takes over — so a drained lane collapses even
  // if it was manually expanded ages ago, while expanding an empty lane still
  // sticks for as long as it stays empty.
  //
  // The phase is a string signature held in state, not a ref: React bails out
  // when it's unchanged, so the common case (a poll where no lane's emptiness
  // moved) costs no extra render, and nothing lags a render behind the value
  // it mirrors.
  const lanePhase = filtered
    ? filtered.columns.map(col => `${col.name}:${col.tasks.length === 0 ? 'empty' : 'full'}`).join('|')
    : null

  const [prevLanePhase, setPrevLanePhase] = useState<null | string>(null)

  useEffect(() => {
    if (lanePhase === null || lanePhase === prevLanePhase) {
      return
    }

    setPrevLanePhase(lanePhase)

    if (prevLanePhase === null) {
      return
    }

    const before = new Map(prevLanePhase.split('|').map(entry => entry.split(':') as [string, string]))
    const overrides = { ...$collapsedLanes.get() }
    let changed = false

    for (const entry of lanePhase.split('|')) {
      const [name, phase] = entry.split(':')
      const was = before.get(name)

      if (was !== undefined && was !== phase && name in overrides) {
        delete overrides[name]
        changed = true
      }
    }

    if (changed) {
      $collapsedLanes.set(overrides)
    }
  }, [lanePhase, prevLanePhase])

  const toggleLane = (name: string, auto: boolean) => {
    const overrides = { ...laneOverrides }
    const next = !(overrides[name] ?? auto)

    if (next === auto) {
      delete overrides[name]
    } else {
      overrides[name] = next
    }

    $collapsedLanes.set(overrides)
  }

  return (
    <div className="relative flex h-full flex-col overflow-hidden bg-(--ui-surface-background)">
      {/* Page-owned titlebar chrome: exists exactly while this page is mounted. */}
      <Contribute area={TITLEBAR_AREAS.center} id="kanban:board-switcher">
        <BoardSwitcher />
      </Contribute>

      <header className="flex shrink-0 flex-wrap items-center gap-2 px-4 py-2">
        <h1 className="text-sm font-semibold text-foreground">{k.title}</h1>
        <span className="rounded-full bg-(--ui-bg-quaternary) px-1.5 py-px text-[0.625rem] tabular-nums text-(--ui-text-tertiary)">
          {total}
        </span>
        {board && (
          <FilterMenu
            archived={archived}
            assignee={assignee}
            board={board}
            onArchived={setArchived}
            onAssignee={setAssignee}
            onTenant={setTenant}
            tenant={tenant}
          />
        )}
        <SearchField aria-label={k.filterCards} onChange={setSearch} placeholder={k.filterCards} value={search} />
        <div className="ml-auto flex items-center gap-1">
          <Tip label={k.orchestrationSettings}>
            <Button
              aria-label={k.orchestrationSettings}
              className={cn(settingsOpen && 'bg-(--ui-control-active-background) text-foreground')}
              onClick={() => setSettingsOpen(!settingsOpen)}
              size="icon-xs"
              variant="ghost"
            >
              <Codicon name="organization" size="0.85rem" />
            </Button>
          </Tip>
          <Button onClick={() => setAddStatus('triage')} size="sm">
            <Codicon name="add" size="0.8rem" />
            {k.newTask}
          </Button>
        </div>
      </header>

      {settingsOpen && <OrchestrationPanel />}

      {board && <Intro />}

      {errorMessage && !board ? (
        <div className="grid flex-1 place-items-center">
          <ErrorState description={errorMessage} title={k.boardLoadError}>
            <Button onClick={() => void boardQuery.refetch()} size="sm" variant="outline">
              <Codicon name="refresh" size="0.8rem" />
              {k.retry}
            </Button>
          </ErrorState>
        </div>
      ) : !filtered ? (
        <div className="grid flex-1 place-items-center">
          <Loader type="lemniscate-bloom" />
        </div>
      ) : total === 0 ? (
        <div className="grid flex-1 place-items-center px-4 text-center">
          <div className="flex flex-col items-center gap-2">
            <Codicon className="text-(--ui-text-quaternary)" name="project" size="1.25rem" />
            <p className="text-xs text-(--ui-text-tertiary)">{search || tenant || assignee ? k.noMatch : k.noTasks}</p>
            <Button className="mt-0.5" onClick={() => setAddStatus('triage')} size="sm" variant="outline">
              <Codicon name="add" size="0.75rem" />
              {k.newTask}
            </Button>
          </div>
        </div>
      ) : (
        <div
          className={cn('flex flex-1 gap-2 overflow-x-auto px-4 pt-1 pb-3', grabbing && 'cursor-grabbing')}
          onMouseDown={onMouseDown}
          ref={lanesRef}
        >
          {filtered.columns.map(col => {
            const auto = boardHasWork && col.tasks.length === 0

            return (
              <Column
                collapsed={laneOverrides[col.name] ?? auto}
                column={col}
                columns={columnNames}
                key={col.name}
                onAdd={setAddStatus}
                onDelete={id => deleteMut.mutate({ archived, board: slug, id })}
                onDropTask={onMove}
                onMove={onMove}
                onOpen={setOpenId}
                onToggle={() => toggleLane(col.name, auto)}
                onToggleSelect={toggleSelect}
                selected={selected}
              />
            )
          })}
        </div>
      )}

      {selected.size > 0 && (
        <SelectionBar
          columns={columnNames}
          generation={selectionGeneration.current}
          isCurrent={(board, generation) =>
            $boardSlug.get() === board && currentSlug.current === board && selectionGeneration.current === generation
          }
          onClear={() => {
            replaceSelection(new Set())
          }}
          onDone={(board, generation, failed) => {
            if (
              $boardSlug.get() === board &&
              currentSlug.current === board &&
              selectionGeneration.current === generation
            ) {
              replaceSelection(new Set(failed))
            }
          }}
          selected={selected}
        />
      )}

      <NewTaskDialog
        onClose={board => board === currentSlug.current && setAddStatus(null)}
        parents={parentOptions}
        target={addStatus}
      />
      <TaskDrawer columns={columnNames} id={openId} onClose={() => setOpenId(null)} onOpen={setOpenId} />
    </div>
  )
}
