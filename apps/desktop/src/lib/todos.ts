export type TodoStatus = 'pending' | 'in_progress' | 'completed' | 'cancelled'
export type CurrentPlanStatus = 'active' | 'paused' | 'completed' | 'superseded' | 'historical'

export interface TodoItem {
  content: string
  id: string
  status: TodoStatus
}

export interface CurrentPlanSnapshot {
  completedCount: number
  hasNewerTurnWithoutTodo: boolean
  items: TodoItem[]
  sourceMessageId: string | null
  status: CurrentPlanStatus
  totalCount: number
  turnNumber: number | null
  updatedAt: number | null
}

export interface CurrentPlanRuntimeState {
  busy: boolean
  hasRuntime: boolean
}

const STATUSES: readonly TodoStatus[] = ['pending', 'in_progress', 'completed', 'cancelled']

const isRecord = (v: unknown): v is Record<string, unknown> => Boolean(v && typeof v === 'object' && !Array.isArray(v))
const isStatus = (v: unknown): v is TodoStatus => (STATUSES as readonly string[]).includes(v as string)

function parseArray(value: unknown[]): TodoItem[] {
  return value.flatMap(item => {
    if (!isRecord(item) || !isStatus(item.status)) {
      return []
    }

    const id = String(item.id ?? '').trim()
    const content = String(item.content ?? '').trim()

    return id && content ? [{ content, id, status: item.status }] : []
  })
}

function parse(value: unknown, depth: number): null | TodoItem[] {
  if (depth > 2) {
    return null
  }

  if (Array.isArray(value)) {
    return parseArray(value)
  }

  if (typeof value === 'string' && value.trim()) {
    try {
      return parse(JSON.parse(value), depth + 1)
    } catch {
      return null
    }
  }

  if (isRecord(value) && Object.hasOwn(value, 'todos')) {
    return parse(value.todos, depth + 1)
  }

  return null
}

export const parseTodos = (value: unknown): null | TodoItem[] => parse(value, 0)

/** Latest parseable todo list from one message's aui content parts (tool-call
 *  parts named `todo`; live parts carry `todos`, hydrated ones args/result). */
export function todosFromMessageContent(content: unknown): null | TodoItem[] {
  if (!Array.isArray(content)) {
    return null
  }

  let latest: null | TodoItem[] = null

  for (const part of content) {
    if (!isRecord(part) || part.type !== 'tool-call' || part.toolName !== 'todo') {
      continue
    }

    const parsed = parseTodos(part.todos) ?? parseTodos(part.result) ?? parseTodos(part.args)

    if (parsed !== null) {
      latest = parsed
    }
  }

  return latest
}

/** Current todo state for a whole transcript — the last list wins. */
export function latestSessionTodos(messages: readonly { parts?: unknown }[]): null | TodoItem[] {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const todos = todosFromMessageContent(messages[i]?.parts)

    if (todos !== null) {
      return todos
    }
  }

  return null
}

interface PlanMessage {
  hidden?: unknown
  id?: unknown
  parts?: unknown
  role?: unknown
  timestamp?: unknown
}

interface TodoSnapshotInMessage {
  items: TodoItem[]
  updatedAt: number | null
}

function finiteTimestamp(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null
}

/** Latest parseable todo snapshot within one rendered message, including the
 * exact persisted tool-result timestamp when hydration supplied it. */
function todoSnapshotFromMessage(message: PlanMessage): TodoSnapshotInMessage | null {
  if (message.hidden || !Array.isArray(message.parts)) {
    return null
  }

  let snapshot: TodoSnapshotInMessage | null = null

  for (const part of message.parts) {
    if (!isRecord(part) || part.type !== 'tool-call' || part.toolName !== 'todo') {
      continue
    }

    const items = parseTodos(part.result)
    const updatedAt = finiteTimestamp(part.todoUpdatedAt)

    // Current Plan is durable history, not an attempted call preview. A result
    // without the persisted tool-row timestamp is still live/ephemeral and must
    // wait for post-turn hydration before it can become the displayed plan.
    if (items !== null && updatedAt !== null) {
      snapshot = { items, updatedAt }
    }
  }

  return snapshot
}

const planIsCompleted = (items: readonly TodoItem[]) =>
  items.length > 0 && items.every(item => item.status === 'completed')

/**
 * Derive the persistent, read-only plan view from hydrated session history.
 *
 * This deliberately does not read the live todo nanostore: message history is
 * the durable source of truth, while explicit runtime state is the only input
 * allowed to produce an `active` label. A stale `in_progress` item alone can
 * therefore never imply liveness.
 */
export function latestSessionPlan(
  messages: readonly PlanMessage[],
  runtime: CurrentPlanRuntimeState
): CurrentPlanSnapshot | null {
  let sourceIndex = -1
  let latest: TodoSnapshotInMessage | null = null

  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const snapshot = todoSnapshotFromMessage(messages[index] ?? {})

    if (snapshot) {
      sourceIndex = index
      latest = snapshot

      break
    }
  }

  if (!latest || sourceIndex < 0 || latest.items.length === 0) {
    return null
  }

  const visibleThroughSource = messages.slice(0, sourceIndex + 1).filter(message => !message.hidden)
  const turnNumber = visibleThroughSource.filter(message => message.role === 'user').length || null

  const hasNewerTurnWithoutTodo = messages
    .slice(sourceIndex + 1)
    .some(message => !message.hidden && message.role === 'user')

  const status: CurrentPlanStatus = hasNewerTurnWithoutTodo
    ? 'superseded'
    : runtime.busy
      ? 'active'
      : !runtime.hasRuntime
        ? 'historical'
        : planIsCompleted(latest.items)
          ? 'completed'
          : 'paused'

  const sourceMessageId = messages[sourceIndex]?.id

  return {
    completedCount: latest.items.filter(item => item.status === 'completed').length,
    hasNewerTurnWithoutTodo,
    items: latest.items,
    sourceMessageId: typeof sourceMessageId === 'string' ? sourceMessageId : null,
    status,
    totalCount: latest.items.length,
    turnNumber,
    updatedAt: latest.updatedAt
  }
}
