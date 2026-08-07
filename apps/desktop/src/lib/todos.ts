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

function parseArray(value: unknown[], strict: boolean): null | TodoItem[] {
  const parsed: TodoItem[] = []

  for (const item of value) {
    if (!isRecord(item) || !isStatus(item.status)) {
      if (strict) {
        return null
      }

      continue
    }

    if (strict && (typeof item.id !== 'string' || typeof item.content !== 'string')) {
      return null
    }

    const id = String(item.id ?? '').trim()
    const content = String(item.content ?? '').trim()

    if (!id || !content) {
      if (strict) {
        return null
      }

      continue
    }

    parsed.push({ content, id, status: item.status })
  }

  return parsed
}

function parse(value: unknown, depth: number, strict: boolean): null | TodoItem[] {
  if (depth > 2) {
    return null
  }

  if (Array.isArray(value)) {
    return parseArray(value, strict)
  }

  if (typeof value === 'string' && value.trim()) {
    try {
      return parse(JSON.parse(value), depth + 1, strict)
    } catch {
      return null
    }
  }

  if (isRecord(value) && Object.hasOwn(value, 'todos')) {
    return parse(value.todos, depth + 1, strict)
  }

  return null
}

export const parseTodos = (value: unknown): null | TodoItem[] => parse(value, 0, false)

/** Parse an authoritative persisted result without silently dropping malformed
 * entries. Empty arrays remain valid clears; any malformed item makes the whole
 * snapshot unusable so older valid history can remain authoritative. */
export const parsePersistedTodos = (value: unknown): null | TodoItem[] => parse(value, 0, true)

/** Whether a rendered message contains a completed, strictly valid todo result.
 * Empty arrays are valid clears and must survive interruption until persisted
 * provenance can be merged back onto the exact tool-call identity. */
export function messageHasValidTodoResult(message: { hidden?: unknown; parts?: unknown }): boolean {
  if (message.hidden || !Array.isArray(message.parts)) {
    return false
  }

  return message.parts.some(
    part =>
      isRecord(part) &&
      part.type === 'tool-call' &&
      part.toolName === 'todo' &&
      Object.hasOwn(part, 'result') &&
      parsePersistedTodos(part.result) !== null
  )
}

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

/** Latest strictly valid todo result within one rendered message. The timestamp
 * remains null until hydration proves persisted provenance; that unproven result
 * still blocks fallback to older history. */
function todoSnapshotFromMessage(message: PlanMessage): TodoSnapshotInMessage | null {
  if (message.hidden || !Array.isArray(message.parts)) {
    return null
  }

  let snapshot: TodoSnapshotInMessage | null = null

  for (const part of message.parts) {
    if (!isRecord(part) || part.type !== 'tool-call' || part.toolName !== 'todo') {
      continue
    }

    const items = parsePersistedTodos(part.result)
    const updatedAt = finiteTimestamp(part.todoUpdatedAt)

    if (items !== null) {
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
      // A newer completed result is authoritative enough to invalidate older
      // history, but not yet proven enough to display until hydration supplies
      // its persisted timestamp.
      if (snapshot.updatedAt === null) {
        return null
      }

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
