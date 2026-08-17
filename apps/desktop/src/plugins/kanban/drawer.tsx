/**
 * Task drawer — the desktop port of the dashboard's task detail, flat-styled:
 * status menu + meta table, DIAGNOSTICS (the "why is this stuck" panel, with
 * reassign recovery), description (editable), result/summary, dependencies,
 * comments (+composer), activity, run history, and the worker log tail.
 */

import {
  Badge,
  Button,
  cn,
  Codicon,
  compactNumber,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  ErrorState,
  host,
  Loader,
  LogView,
  Textarea,
  Tip,
  useMutation,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import {
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState
} from 'react'

import {
  $boardSlug,
  addComment,
  assignTask,
  BOARDS_KEY,
  deleteTask,
  estimateTask,
  fetchLog,
  fetchTask,
  logKey,
  patchTask,
  profileQueryOptions,
  reclaimTask,
  taskKey,
  uploadAttachment
} from './api'
import { ModelOverrideField, overridePatch } from './model-override'
import {
  type Diagnostic,
  type DiagnosticAction,
  type KanbanAttachment,
  type KanbanEvent,
  type KanbanTaskDetail,
  SEVERITY_TONE,
  type TaskEstimate
} from './types'
import {
  ago,
  Avatar,
  Callout,
  columnLabel,
  duration,
  errText,
  isLockedTarget,
  type KanbanText,
  lockedReason,
  ScrollFade,
  Section,
  shortId,
  StatusMenu,
  useDefaultAssignee,
  useKanban
} from './ui'

/**
 * Turn a task_events row into an operator-readable line. The backend logs
 * machine payloads ("status" + {"status":"ready"}); rendering the raw kind
 * made the feed useless ("status · 2 sec. ago" after a drag). Known kinds get
 * prose with the payload folded in; unknown kinds fall back to kind + compact
 * key=value detail so new backend events still say something.
 */
function eventText(event: KanbanEvent, k: KanbanText): { detail?: string; label: string } {
  let p: Record<string, unknown> = {}

  if (typeof event.payload === 'string' && event.payload) {
    try {
      p = JSON.parse(event.payload) as Record<string, unknown>
    } catch {
      return { label: event.kind.replace(/_/g, ' '), detail: event.payload }
    }
  } else if (event.payload && typeof event.payload === 'object') {
    p = event.payload as Record<string, unknown>
  }

  const str = (key: string): null | string => {
    const value = p[key]

    return typeof value === 'string' && value ? value : null
  }

  const col = (key: string) => {
    const value = str(key)

    return value ? columnLabel(k, value) : null
  }

  switch (event.kind) {
    case 'created':
      return { label: k.evtCreated(col('status') ?? '', str('assignee') ?? '') }
    case 'status': {
      const reason = str('reason')

      return {
        label: k.evtMovedTo(col('status') ?? '?'),
        detail: reason === 'parent_reopened' ? k.evtParentReopened(str('parent') ?? '') : (reason ?? undefined)
      }
    }

    case 'assigned': {
      const assignee = str('assignee')

      return { label: assignee ? k.evtAssignedTo(assignee) : k.evtUnassigned }
    }

    case 'commented':
      return { label: k.evtCommentBy(str('author') ?? k.someone) }

    case 'claimed':
      return { label: str('source_status') === 'review' ? k.evtClaimedReview : k.evtClaimedWorker }

    case 'spawned':
      return { label: k.evtWorkerStarted, detail: p.pid != null ? `pid ${p.pid}` : undefined }

    case 'completed':
      return { label: k.evtCompleted }

    case 'blocked':
      return { label: k.evtBlocked, detail: str('reason') ?? undefined }

    case 'unblocked':
      return { label: k.evtUnblocked(col('status') ?? '') }

    case 'reclaimed':
      return { label: k.evtReclaimed, detail: str('reason') ?? undefined }

    case 'specified':
      return { label: k.evtSpecified }

    case 'promoted':
      return { label: k.evtPromoted }

    case 'scheduled':
      return { label: k.evtScheduled, detail: str('reason') ?? undefined }

    case 'archived':
      return { label: k.evtArchived }

    case 'reprioritized':
      return { label: k.evtReprioritized(String(p.priority ?? '?')) }
    default: {
      const detail = Object.entries(p)
        .filter(([, value]) => value != null && typeof value !== 'object')
        .map(([key, value]) => `${key}=${String(value)}`)
        .join(' ')

      return { label: event.kind.replace(/_/g, ' '), detail: detail || undefined }
    }
  }
}

function MetaRow({ children, label }: { children: ReactNode; label: string }) {
  return (
    <>
      <span className="text-(--ui-text-quaternary)">{label}</span>
      <span className="min-w-0 truncate text-(--ui-text-secondary)">{children}</span>
    </>
  )
}

/** The dashboard's diagnostics panel: severity-toned, plain-English, with the
 *  backend's structured recovery actions as buttons. `reassign` is skipped —
 *  the Assignee control in the meta table IS that action, inline. */
function Diagnostics({ items, onReclaim }: { items: Diagnostic[]; onReclaim: () => void }) {
  const k = useKanban()

  const act = (action: DiagnosticAction) => {
    if (action.kind === 'reclaim') {
      onReclaim()
    } else if (action.kind === 'cli_hint') {
      void navigator.clipboard.writeText(String(action.payload?.command ?? action.label))
      host.notify({ kind: 'info', message: k.commandCopied })
    }
  }

  return (
    <div className="flex flex-col gap-2">
      {items.map(diag => {
        const tone = SEVERITY_TONE[diag.severity]
        const actions = diag.actions.filter(action => action.kind === 'reclaim' || action.kind === 'cli_hint')

        return (
          <Callout
            key={`${diag.kind}-${diag.last_seen_at}`}
            title={`${diag.title}${diag.count > 1 ? ` ×${diag.count}` : ''}`}
            tone={tone}
          >
            <p className="whitespace-pre-wrap text-[0.71rem] leading-relaxed text-(--ui-text-secondary)">
              {diag.detail}
            </p>
            {actions.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {actions.map(action => (
                  <Button
                    key={`${action.kind}-${action.label}`}
                    onClick={() => act(action)}
                    size="xs"
                    variant={action.suggested ? 'secondary' : 'outline'}
                  >
                    {action.kind === 'cli_hint' && <Codicon name="copy" size="0.7rem" />}
                    {action.label}
                  </Button>
                ))}
              </div>
            )}
          </Callout>
        )
      })}
    </div>
  )
}

/** Jira-style inline assignee editor: the meta row IS the control — click the
 *  assignee to reassign (reclaims a running worker first, resets the failure
 *  streak — the explicit human recovery action). */
function AssigneeMenu({
  busy,
  current,
  onAssign
}: {
  busy: boolean
  current: null | string | undefined
  onAssign: (profile: null | string) => void
}) {
  const k = useKanban()
  const slug = useValue($boardSlug)
  const { data: roster } = useQuery(profileQueryOptions(slug))
  const effectiveProfiles = (roster?.profiles ?? []).filter(profile => profile.effective_allowed)

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          aria-label={current ?? k.unassigned}
          className="-mx-1 inline-flex max-w-full items-center gap-1.5 rounded px-1 py-0.5 text-left transition-colors hover:bg-(--chrome-action-hover)"
          disabled={busy}
          type="button"
        >
          {current ? (
            <>
              <Avatar name={current} size="0.875rem" />
              <span className="truncate">{current}</span>
            </>
          ) : (
            <span className="text-(--ui-text-quaternary)">{k.unassigned}</span>
          )}
          <Codicon className="shrink-0 text-(--ui-text-quaternary)" name="chevron-down" size="0.65rem" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start">
        {effectiveProfiles.map(profile => (
          <DropdownMenuItem disabled={busy} key={profile.name} onSelect={() => onAssign(profile.name)}>
            <Avatar name={profile.name} size="0.875rem" />
            {profile.name}
            {profile.name === current && <Codicon className="ml-auto" name="check" size="0.8rem" />}
          </DropdownMenuItem>
        ))}
        {effectiveProfiles.length > 0 && <DropdownMenuSeparator />}
        <DropdownMenuItem disabled={busy || !current} onSelect={() => onAssign(null)}>
          {current ? k.unassignAction : k.unassigned}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

// Mirrors the review pane's commit-message field: one row tall to start
// (button-height), CSS field-sizing grows it with content, button hugs the
// bottom edge as it grows.
//
// On a RUNNING task the worker polls its comment thread and folds new notes
// into the live turn (OUT-OF-BAND steer), so a plain note reaches the agent
// mid-run within a few seconds — no block/unblock dance. `onRequeue` is the
// heavier option: post the note AND reclaim so the task restarts from scratch
// with the note in context (use when the current run has gone off the rails).
function CommentComposer({
  onRequeue,
  onSubmit,
  pending,
  running
}: {
  onRequeue?: (body: string) => void
  onSubmit: (body: string) => void
  pending: boolean
  running?: boolean
}) {
  const k = useKanban()
  const [body, setBody] = useState('')

  const submit = () => {
    const trimmed = body.trim()

    if (trimmed && !pending) {
      onSubmit(trimmed)
      setBody('')
    }
  }

  const requeue = () => {
    const trimmed = body.trim()

    if (trimmed && !pending && onRequeue) {
      onRequeue(trimmed)
      setBody('')
    }
  }

  return (
    <div className="flex flex-col gap-1.5">
      <div className="relative">
        <Textarea
          className={cn('field-sizing-content max-h-40 min-h-0 resize-none', running ? 'pr-[3.5rem]' : 'pr-[5rem]')}
          onChange={event => setBody(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              submit()
            }
          }}
          placeholder={running ? k.messageWorker : k.addComment}
          rows={1}
          size="sm"
          value={body}
        />
        <Button
          className="absolute top-1 right-1"
          disabled={!body.trim() || pending}
          onClick={submit}
          size="xs"
          variant="secondary"
        >
          {running ? k.send : k.comment}
        </Button>
      </div>
      {running && onRequeue && (
        <div className="flex items-center justify-between gap-2">
          <span className="text-[0.625rem] leading-tight text-(--ui-text-quaternary)">{k.deliveredLive}</span>
          <Button className="shrink-0" disabled={!body.trim() || pending} onClick={requeue} size="xs" variant="outline">
            <Codicon name="debug-restart" size="0.7rem" />
            {k.requeueWithNote}
          </Button>
        </div>
      )}
    </div>
  )
}

function DescriptionSection({ body, onSave }: { body: null | string | undefined; onSave: (body: string) => void }) {
  const k = useKanban()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')

  return (
    <Section
      action={
        <Button
          aria-label={editing ? k.cancelEdit : k.editDescription}
          onClick={() => {
            setDraft(body ?? '')
            setEditing(!editing)
          }}
          size="icon-xs"
          variant="ghost"
        >
          <Codicon name={editing ? 'close' : 'edit'} size="0.75rem" />
        </Button>
      }
      label={k.description}
    >
      {editing ? (
        <div className="flex flex-col gap-1.5">
          <Textarea
            className="min-h-24 text-[0.75rem]"
            onChange={event => setDraft(event.target.value)}
            value={draft}
          />
          <Button
            className="self-end"
            onClick={() => {
              onSave(draft)
              setEditing(false)
            }}
            size="xs"
            variant="secondary"
          >
            {k.save}
          </Button>
        </div>
      ) : body ? (
        <p className="whitespace-pre-wrap text-[0.8125rem] text-(--ui-text-secondary)">{body}</p>
      ) : (
        <p className="text-[0.8125rem] text-(--ui-text-quaternary)">{k.noDescription}</p>
      )}
    </Section>
  )
}

// `latest_summary` is just the newest non-null run summary. A reclaim writes an
// administrative note into that slot; hide those (Runs still shows them).
const isAdminSummary = (summary: string) => /^status changed to \w+ \(dashboard\/direct\)$/.test(summary)

function AttachmentsSection({
  attachments,
  onUpload,
  pending
}: {
  attachments: KanbanAttachment[]
  onUpload: (file: File) => void
  pending: boolean
}) {
  const k = useKanban()
  const fileRef = useRef<HTMLInputElement>(null)

  return (
    <Section
      action={
        <>
          <input
            hidden
            onChange={event => {
              const file = event.target.files?.[0]

              if (file) {
                onUpload(file)
              }

              event.target.value = ''
            }}
            ref={fileRef}
            type="file"
          />
          <Button
            aria-label={k.uploadAttachment}
            disabled={pending}
            onClick={() => fileRef.current?.click()}
            size="icon-xs"
            variant="ghost"
          >
            <Codicon name={pending ? 'sync' : 'cloud-upload'} size="0.8rem" spinning={pending} />
          </Button>
        </>
      }
      label={k.attachments(attachments.length)}
    >
      {attachments.length > 0 ? (
        <ul className="flex flex-col gap-1">
          {attachments.map(attachment => (
            <li className="flex items-center gap-1.5 text-[0.75rem] text-(--ui-text-tertiary)" key={attachment.id}>
              <Codicon name="file" size="0.75rem" />
              {attachment.filename}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-[0.75rem] text-(--ui-text-quaternary)">{k.noAttachments}</p>
      )}
    </Section>
  )
}

// Rough effort estimate via the auxiliary (auto-routed) model. Tokens +
// complexity, never dollars — providers don't report cost reliably. Gated
// behind an explicit click + disclaimer since it makes a model call. The
// control keeps a stable footprint (spinner swaps in place) so there's no
// layout jump when it runs.
interface DrawerAction {
  board: string
  generation: number
  taskId: string
}

interface AssignmentOperation extends DrawerAction {
  owner: symbol
  profile: null | string
}

const DRAWER_FOCUSABLE = 'a[href], button, input, select, textarea, [tabindex]'

function getFocusable(container: HTMLElement): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>(DRAWER_FOCUSABLE)].filter(
    element =>
      element.tabIndex >= 0 &&
      !element.matches(':disabled, input[type="hidden"]') &&
      !element.closest('[hidden], [aria-hidden="true"]')
  )
}

function EstimateSection({
  action,
  isCurrent
}: {
  action: DrawerAction
  isCurrent: (action: DrawerAction) => boolean
}) {
  const k = useKanban()
  const [result, setResult] = useState<null | TaskEstimate>(null)

  const est = useMutation({
    mutationFn: (candidate: DrawerAction) => estimateTask(candidate.board, candidate.taskId),
    onError: (err, candidate) => {
      if (isCurrent(candidate)) {
        host.notify({ kind: 'error', message: errText(err) })
      }
    },
    onSuccess: (r, candidate) => {
      if (!isCurrent(candidate)) {
        return
      }

      if (r.ok) {
        setResult(r)
      } else {
        host.notify({ kind: 'warning', message: r.reason || k.couldNotEstimate })
      }
    }
  })

  // Every drawer lifecycle gets a clean estimate, including close/reopen with
  // the same board/task identity.
  useEffect(() => setResult(null), [action.generation])

  const estimating = est.isPending && Boolean(est.variables && isCurrent(est.variables))

  return (
    <Section label={k.estimate}>
      {result?.ok ? (
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-2 text-[0.8125rem]">
            <span className="font-medium tabular-nums text-(--ui-text-secondary)">
              ~{compactNumber(result.est_tokens)} {k.tokUnit}
            </span>
            {result.complexity && (
              <span className="text-(--ui-text-tertiary)">
                · {k.complexity[result.complexity] ?? result.complexity}
              </span>
            )}
            <Tip label={k.reEstimate}>
              <Button
                aria-label={k.reEstimate}
                className="ml-auto"
                disabled={estimating}
                onClick={() => est.mutate(action)}
                size="icon-xs"
                variant="ghost"
              >
                <Codicon name="refresh" size="0.75rem" spinning={estimating} />
              </Button>
            </Tip>
          </div>
          {result.rationale && (
            <p className="text-[0.6875rem] leading-relaxed text-(--ui-text-quaternary)">{result.rationale}</p>
          )}
        </div>
      ) : (
        <div className="flex items-center gap-2">
          <Button disabled={estimating} onClick={() => est.mutate(action)} size="xs" variant="outline">
            <Codicon name={estimating ? 'loading' : 'dashboard'} size="0.75rem" spinning={estimating} />
            {estimating ? k.estimating : k.estimateEffort}
          </Button>
          <Tip label={k.estimateTipLong}>
            <span className="text-[0.625rem] text-(--ui-text-quaternary)">{k.makesModelCall}</span>
          </Tip>
        </div>
      )}
    </Section>
  )
}

export function TaskDrawer({
  columns,
  id,
  onClose,
  onOpen
}: {
  columns: string[]
  id: null | string
  onClose: () => void
  onOpen: (id: string) => void
}) {
  const k = useKanban()
  const qc = useQueryClient()
  const slug = useValue($boardSlug)
  const drawerOpen = Boolean(id)
  const lifecycle = useRef({ board: slug, generation: 0, open: drawerOpen, taskId: id })
  const assignmentOwner = useRef<null | symbol>(null)
  const drawerRef = useRef<HTMLDivElement>(null)
  const closeButtonRef = useRef<HTMLButtonElement>(null)
  const titleId = useId()

  if (lifecycle.current.board !== slug || lifecycle.current.open !== drawerOpen || lifecycle.current.taskId !== id) {
    lifecycle.current = {
      board: slug,
      generation: lifecycle.current.generation + 1,
      open: drawerOpen,
      taskId: id
    }
  }

  const action: DrawerAction | null = id ? { board: slug, generation: lifecycle.current.generation, taskId: id } : null

  const isCurrent = useCallback((candidate: DrawerAction) => {
    const current = lifecycle.current

    return (
      current.open &&
      current.board === candidate.board &&
      current.generation === candidate.generation &&
      current.taskId === candidate.taskId
    )
  }, [])

  const closeDrawer = useCallback(
    (candidate: DrawerAction) => {
      if (!isCurrent(candidate)) {
        return
      }

      const current = lifecycle.current
      lifecycle.current = {
        board: current.board,
        generation: current.generation + 1,
        open: false,
        taskId: null
      }
      onClose()
    },
    [isCurrent, onClose]
  )

  const openDrawerTask = (taskId: string) => {
    const current = lifecycle.current

    if (!current.open) {
      return
    }

    lifecycle.current = {
      board: slug,
      generation: current.generation + 1,
      open: true,
      taskId
    }
    onOpen(taskId)
  }

  // Socket-invalidated (bindApi); the interval is only the socketless heartbeat.
  const detailQuery = useQuery({
    enabled: !!id,
    queryFn: () => fetchTask(slug, id!),
    queryKey: taskKey(slug, id ?? ''),
    refetchInterval: 30_000
  })

  const { data: detail, error } = detailQuery

  const task = detail?.task
  const running = task?.status === 'running'
  const defaultAssignee = useDefaultAssignee()

  const { data: log } = useQuery({
    enabled: !!id,
    queryFn: () => fetchLog(slug, id!),
    queryKey: logKey(slug, id ?? ''),
    refetchInterval: running ? 3_000 : 15_000
  })

  // Move focus into the modal drawer. Restore the prior target only when focus
  // is still owned by this drawer (or fell back to body) so cleanup never steals
  // a deliberate focus move elsewhere.
  useEffect(() => {
    if (!drawerOpen) {
      return
    }

    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const drawer = drawerRef.current
    closeButtonRef.current?.focus()

    return () => {
      const active = document.activeElement

      if (
        previous?.isConnected &&
        (active === document.body || (active instanceof Node && Boolean(drawer?.contains(active))))
      ) {
        previous.focus()
      }
    }
  }, [drawerOpen])

  // Capture Escape before shell shortcuts so one key closes exactly one modal.
  useEffect(() => {
    if (!drawerOpen) {
      return
    }

    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        const drawer = drawerRef.current
        const active = document.activeElement

        // Portaled menus/selects own their first Escape. Their focused content
        // sits outside the drawer node even though it belongs to this dialog.
        if (active instanceof Node && active !== document.body && !drawer?.contains(active)) {
          return
        }

        event.preventDefault()
        event.stopPropagation()

        const current = lifecycle.current

        if (current.open && current.taskId) {
          closeDrawer({ board: current.board, generation: current.generation, taskId: current.taskId })
        }
      }
    }

    window.addEventListener('keydown', onKey, true)

    return () => window.removeEventListener('keydown', onKey, true)
  }, [closeDrawer, drawerOpen])

  const trapFocus = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'Tab') {
      return
    }

    const drawer = drawerRef.current
    const focusable = drawer ? getFocusable(drawer) : []

    if (focusable.length === 0) {
      event.preventDefault()

      return
    }

    const first = focusable[0]
    const last = focusable.at(-1)!
    const active = document.activeElement

    if (event.shiftKey ? active === first || !drawer?.contains(active) : active === last || !drawer?.contains(active)) {
      event.preventDefault()
      ;(event.shiftKey ? last : first).focus()
    }
  }

  const invalidate = (board = slug, taskId = id!, countChanged = false) => {
    void qc.invalidateQueries({ queryKey: taskKey(board, taskId) })
    void qc.invalidateQueries({ queryKey: ['kanban', 'board', board] })

    if (countChanged) {
      void qc.invalidateQueries({ queryKey: BOARDS_KEY })
    }
  }

  const assignment = useMutation({
    mutationFn: ({ board, profile, taskId }: AssignmentOperation) => assignTask(board, taskId, profile),
    onError: (err, vars, context) => {
      if (context?.optimistic && context.previous && isCurrent(vars)) {
        qc.setQueryData(context.key, context.previous)
      }

      if (isCurrent(vars)) {
        host.notify({ kind: 'error', message: errText(err) })
      }
    },
    onMutate: async (vars: AssignmentOperation) => {
      const key = taskKey(vars.board, vars.taskId)
      await qc.cancelQueries({ queryKey: key })
      const previous = qc.getQueryData<KanbanTaskDetail>(key)
      const optimistic = Boolean(previous && isCurrent(vars))

      if (previous && optimistic) {
        qc.setQueryData(key, { ...previous, task: { ...previous.task, assignee: vars.profile } })
      }

      return { key, optimistic, previous }
    },
    onSettled: (_data, _error, vars) => {
      if (assignmentOwner.current === vars.owner) {
        assignmentOwner.current = null
      }

      invalidate(vars.board, vars.taskId)
    }
  })

  // Optimistic status change against the task cache; rolls back + toasts on a
  // rejected transition (the backend enforces the workflow).
  const moveMut = useMutation({
    mutationFn: ({ board, status, taskId }: DrawerAction & { fromStatus: string; status: string }) =>
      patchTask(board, taskId, { status }),
    onMutate: async ({ board, status, taskId }) => {
      const key = taskKey(board, taskId)
      await qc.cancelQueries({ queryKey: key })
      const previous = qc.getQueryData<KanbanTaskDetail>(key)

      if (previous) {
        qc.setQueryData(key, { ...previous, task: { ...previous.task, status } })
      }

      return { key, previous }
    },
    onError: (err, vars, context) => {
      if (context?.previous && isCurrent(vars)) {
        qc.setQueryData(context.key, context.previous)
      }

      if (isCurrent(vars)) {
        host.notify({ kind: 'error', message: errText(err) })
      }
    },
    onSettled: (_data, _error, vars) =>
      invalidate(vars.board, vars.taskId, vars.fromStatus === 'archived' || vars.status === 'archived')
  })

  const mutate =
    (
      fn: (candidate: DrawerAction) => Promise<unknown>,
      onDone?: (candidate: DrawerAction) => void,
      countChanged = false
    ) =>
    () => {
      const candidate = action

      if (!candidate) {
        return
      }

      void fn(candidate).then(
        () => {
          invalidate(candidate.board, candidate.taskId, countChanged)

          if (isCurrent(candidate)) {
            onDone?.(candidate)
          }
        },
        (err: unknown) => {
          if (isCurrent(candidate)) {
            host.notify({ kind: 'error', message: errText(err) })
          }
        }
      )
    }

  const commentMut = useMutation({
    mutationFn: ({ board, body, taskId }: DrawerAction & { body: string }) => addComment(board, taskId, body),
    onError: (err, vars) => {
      if (isCurrent(vars)) {
        host.notify({ kind: 'error', message: errText(err) })
      }
    },
    onSuccess: (_data, vars) => invalidate(vars.board, vars.taskId)
  })

  // "Note & requeue" for a running task: post the note, then reclaim so the
  // dispatcher re-runs it with the note in the worker's context — the one-click
  // replacement for the block → comment → unblock dance.
  const requeueMut = useMutation({
    mutationFn: async ({ board, body, taskId }: DrawerAction & { body: string }) => {
      await addComment(board, taskId, body)
      await reclaimTask(board, taskId)
    },
    onError: (err, vars) => {
      if (isCurrent(vars)) {
        host.notify({ kind: 'error', message: errText(err) })
      }
    },
    onSuccess: (_data, vars) => {
      invalidate(vars.board, vars.taskId)

      if (isCurrent(vars)) {
        host.notify({ kind: 'info', message: k.notePosted })
      }
    }
  })

  const uploadMut = useMutation({
    mutationFn: async ({ board, file, taskId }: DrawerAction & { file: File }) =>
      uploadAttachment(board, taskId, {
        bytes: await file.arrayBuffer(),
        contentType: file.type || undefined,
        filename: file.name
      }),
    onError: (err, vars) => {
      if (isCurrent(vars)) {
        host.notify({ kind: 'error', message: errText(err) })
      }
    },
    onSuccess: (_data, vars) => invalidate(vars.board, vars.taskId)
  })

  if (!id) {
    return null
  }

  const currentAction = action!

  const assignmentBusy = assignmentOwner.current !== null

  const commentPending =
    (commentMut.isPending && Boolean(commentMut.variables && isCurrent(commentMut.variables))) ||
    (requeueMut.isPending && Boolean(requeueMut.variables && isCurrent(requeueMut.variables)))

  const uploadPending = uploadMut.isPending && Boolean(uploadMut.variables && isCurrent(uploadMut.variables))
  const errorMessage = error ? errText(error) : null

  const move = (status: string) => {
    if (!task || status === task.status) {
      return
    }

    if (isLockedTarget(status)) {
      host.notify({ kind: 'info', message: lockedReason(k, status) })

      return
    }

    if (action) {
      moveMut.mutate({ ...action, fromStatus: task.status, status })
    }
  }

  const assign = (profile: null | string) => {
    if (assignmentOwner.current || (task?.assignee ?? null) === profile) {
      return
    }

    const owner = Symbol('kanban-drawer-assignment')
    assignmentOwner.current = owner
    assignment.mutate({ ...currentAction, owner, profile })
  }

  return (
    <div
      aria-labelledby={titleId}
      aria-modal="true"
      className="absolute inset-y-0 right-0 z-20 flex w-[26rem] flex-col border-l border-(--ui-stroke-tertiary) bg-(--ui-bg-elevated) duration-150 ease-out animate-in fade-in slide-in-from-right-4"
      onKeyDown={trapFocus}
      ref={drawerRef}
      role="dialog"
    >
      <header className="flex flex-col gap-2 px-4 pt-3.5 pb-3">
        <div className="flex items-center gap-2">
          {task ? (
            <StatusMenu columns={columns} onMove={move} status={task.status} />
          ) : (
            <span className="font-mono text-sm text-(--ui-text-tertiary)">{shortId(id)}</span>
          )}
          {task && (
            <span className="font-mono text-[0.625rem] text-(--ui-text-quaternary)" data-selectable-text="true">
              {shortId(task.id)}
            </span>
          )}
          <div className="ml-auto flex items-center gap-0.5">
            {task && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button
                    aria-label={k.taskActions}
                    className="grid size-6 place-items-center rounded text-(--ui-text-tertiary) transition-colors hover:bg-(--chrome-action-hover) hover:text-foreground"
                    type="button"
                  >
                    <Codicon name="ellipsis" size="0.9rem" />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  <DropdownMenuItem
                    onSelect={() => {
                      void navigator.clipboard.writeText(task.id)
                      host.notify({ kind: 'info', message: k.copiedId(task.id) })
                    }}
                  >
                    <Codicon name="copy" size="0.85rem" />
                    {k.copyTaskId}
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    onSelect={() => {
                      void navigator.clipboard.writeText(task.title || task.id)
                      host.notify({ kind: 'info', message: k.copiedTitle })
                    }}
                  >
                    <Codicon name="copy" size="0.85rem" />
                    {k.copyTitle}
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem
                    onSelect={mutate(
                      candidate => patchTask(candidate.board, candidate.taskId, { status: 'archived' }),
                      closeDrawer,
                      true
                    )}
                  >
                    <Codicon name="archive" size="0.85rem" />
                    {k.archiveTask}
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    className="text-destructive"
                    onSelect={mutate(candidate => deleteTask(candidate.board, candidate.taskId), closeDrawer, true)}
                  >
                    <Codicon name="trash" size="0.85rem" />
                    {k.deleteTask}
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            )}
            <button
              aria-label={k.close}
              className="grid size-6 place-items-center rounded text-(--ui-text-tertiary) transition-colors hover:bg-(--chrome-action-hover) hover:text-foreground"
              onClick={() => closeDrawer(currentAction)}
              ref={closeButtonRef}
              type="button"
            >
              <Codicon name="close" size="0.9rem" />
            </button>
          </div>
        </div>
        <h2 className="text-sm leading-snug font-semibold text-foreground" data-selectable-text="true" id={titleId}>
          {task?.title || task?.id || shortId(id)}
        </h2>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-4" data-selectable-text="true">
        {errorMessage && !detail ? (
          <ErrorState description={errorMessage} title={k.taskLoadError}>
            <Button onClick={() => void detailQuery.refetch()} size="sm" variant="outline">
              <Codicon name="refresh" size="0.8rem" />
              {k.retry}
            </Button>
          </ErrorState>
        ) : !detail || !task ? (
          <div className="grid h-32 place-items-center">
            <Loader type="lemniscate-bloom" />
          </div>
        ) : (
          <div className="flex flex-col gap-4 text-sm">
            <div className="grid grid-cols-[6rem_minmax(0,1fr)] gap-x-3 gap-y-1 text-[0.71rem]">
              <MetaRow label={k.assignee}>
                <AssigneeMenu busy={assignmentBusy} current={task.assignee} onAssign={assign} />
              </MetaRow>
              {typeof task.priority === 'number' && <MetaRow label={k.metaPriority}>{task.priority}</MetaRow>}
              {task.tenant && <MetaRow label={k.metaTenant}>{task.tenant}</MetaRow>}
              {task.workspace_path && (
                <MetaRow label={k.workspace}>
                  {task.workspace_kind ? `${task.workspace_kind}: ` : ''}
                  {task.workspace_path}
                </MetaRow>
              )}
              <MetaRow label={k.model}>
                <ModelOverrideField
                  onChange={next =>
                    void mutate(candidate => patchTask(candidate.board, candidate.taskId, overridePatch(next)))()
                  }
                  value={{
                    effort: task.reasoning_effort ?? '',
                    model: task.model_override ?? '',
                    provider: task.provider_override ?? ''
                  }}
                />
              </MetaRow>
              {task.created_by && <MetaRow label={k.metaCreatedBy}>{task.created_by}</MetaRow>}
              {ago(task.created_at) && <MetaRow label={k.metaCreated}>{ago(task.created_at)}</MetaRow>}
              {running && task.worker_pid ? <MetaRow label={k.metaWorkerPid}>{task.worker_pid}</MetaRow> : null}
            </div>

            {task.status === 'ready' && !task.assignee && !defaultAssignee && (
              <Callout title={k.readyUnassignedTitle} tone={SEVERITY_TONE.warning}>
                <p className="text-[0.71rem] leading-relaxed text-(--ui-text-secondary)">{k.readyUnassignedBody}</p>
              </Callout>
            )}

            {task.diagnostics && task.diagnostics.length > 0 && (
              <Section label={k.diagnosticsN(task.diagnostics.length)}>
                <Diagnostics
                  items={task.diagnostics}
                  onReclaim={() => void mutate(candidate => reclaimTask(candidate.board, candidate.taskId))()}
                />
              </Section>
            )}

            <DescriptionSection
              body={task.body}
              onSave={body => void mutate(candidate => patchTask(candidate.board, candidate.taskId, { body }))()}
            />

            <EstimateSection action={currentAction} isCurrent={isCurrent} />

            {task.result && (
              <Section label={k.result}>
                <p className="whitespace-pre-wrap text-[0.8125rem] text-(--ui-text-secondary)">{task.result}</p>
              </Section>
            )}

            {task.latest_summary && !isAdminSummary(task.latest_summary) && (
              <Section label={k.latestSummary}>
                <p className="whitespace-pre-wrap text-[0.8125rem] text-(--ui-text-secondary)">{task.latest_summary}</p>
              </Section>
            )}

            {(detail.links.parents.length > 0 || detail.links.children.length > 0) && (
              <Section label={k.dependencies}>
                {(['parents', 'children'] as const).map(side =>
                  detail.links[side].length > 0 ? (
                    <div className="flex flex-wrap items-center gap-1.5" key={side}>
                      <span className="text-[0.6875rem] text-(--ui-text-quaternary)">
                        {side === 'parents' ? k.blockedBy : k.blocks}
                      </span>
                      {detail.links[side].map(linked => (
                        <button
                          className="rounded bg-(--ui-bg-quaternary) px-1.5 py-0.5 font-mono text-[0.625rem] text-(--ui-text-secondary) transition-colors hover:bg-(--chrome-action-hover) hover:text-foreground"
                          key={linked}
                          onClick={() => openDrawerTask(linked)}
                          type="button"
                        >
                          {shortId(linked)}
                        </button>
                      ))}
                    </div>
                  ) : null
                )}
              </Section>
            )}

            <Section
              action={
                <Tip label={running ? k.commentsHelpRunning : k.commentsHelp}>
                  <span className="grid size-5 place-items-center rounded text-(--ui-text-quaternary) hover:text-(--ui-text-secondary)">
                    <Codicon name="question" size="0.8rem" />
                  </span>
                </Tip>
              }
              label={k.comments(detail.comments.length)}
            >
              {detail.comments.length > 0 && (
                <ul className="flex flex-col gap-2">
                  {detail.comments.map(comment => (
                    <li className="text-[0.75rem]" key={comment.id}>
                      <span className="font-medium text-(--ui-text-secondary)">{comment.author}</span>
                      <span className="ml-2 text-[0.625rem] text-(--ui-text-quaternary)">
                        {ago(comment.created_at)}
                      </span>
                      <p className="whitespace-pre-wrap text-(--ui-text-tertiary)">{comment.body}</p>
                    </li>
                  ))}
                </ul>
              )}
              <CommentComposer
                onRequeue={body => requeueMut.mutate({ ...currentAction, body })}
                onSubmit={body => commentMut.mutate({ ...currentAction, body })}
                pending={commentPending}
                running={running}
              />
            </Section>

            {detail.events.length > 0 && (
              <Section label={k.activity(detail.events.length)}>
                <ScrollFade deps={detail.events.length} max="7rem">
                  <ul className="flex flex-col gap-1">
                    {detail.events.map(event => {
                      const { detail: extra, label } = eventText(event, k)

                      return (
                        <li className="flex items-baseline gap-2 text-[0.6875rem]" key={event.id}>
                          <span className="shrink-0 text-(--ui-text-secondary)">{label}</span>
                          {extra && (
                            <span
                              className="min-w-0 truncate text-[0.625rem] text-(--ui-text-quaternary)"
                              title={extra}
                            >
                              {extra}
                            </span>
                          )}
                          <span className="ml-auto shrink-0 text-(--ui-text-quaternary)">{ago(event.created_at)}</span>
                        </li>
                      )
                    })}
                  </ul>
                </ScrollFade>
              </Section>
            )}

            {detail.runs.length > 0 && (
              <Section label={k.runs(detail.runs.length)}>
                <ScrollFade max="11rem">
                  <ul className="flex flex-col gap-1.5">
                    {detail.runs.map(run => {
                      const failed = ['crashed', 'failed', 'timed_out', 'gave_up'].includes(run.outcome ?? run.status)

                      return (
                        <li className="flex flex-col gap-0.5 text-[0.71rem]" key={run.id}>
                          <div className="flex items-center gap-2">
                            <Badge size="xs" variant={failed ? 'destructive' : 'muted'}>
                              {run.outcome ?? run.status}
                            </Badge>
                            {run.profile && <span className="text-(--ui-text-tertiary)">{run.profile}</span>}
                            {duration(run.started_at, run.ended_at) && (
                              <span className="text-(--ui-text-quaternary)">
                                {duration(run.started_at, run.ended_at)}
                              </span>
                            )}
                            <span className="ml-auto shrink-0 text-(--ui-text-quaternary)">
                              {ago(run.ended_at ?? run.started_at)}
                            </span>
                          </div>
                          {(run.error || run.summary) && (
                            <p
                              className={cn(
                                'line-clamp-2 whitespace-pre-wrap',
                                run.error ? 'text-destructive' : 'text-(--ui-text-quaternary)'
                              )}
                            >
                              {run.error ?? run.summary}
                            </p>
                          )}
                        </li>
                      )
                    })}
                  </ul>
                </ScrollFade>
              </Section>
            )}

            {log?.exists && log.content && (
              <Section label={log.truncated ? k.workerLogTail : k.workerLog}>
                <ScrollFade deps={log.content.length} max="12rem">
                  <LogView className="border-0 px-0">{log.content}</LogView>
                </ScrollFade>
              </Section>
            )}

            <AttachmentsSection
              attachments={detail.attachments}
              onUpload={file => uploadMut.mutate({ ...currentAction, file })}
              pending={uploadPending}
            />
          </div>
        )}
      </div>
    </div>
  )
}
