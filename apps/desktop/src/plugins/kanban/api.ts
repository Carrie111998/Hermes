/**
 * Kanban data layer. Everything goes through `ctx.rest` — the plugin's own
 * `/api/plugins/kanban/*` FastAPI router (`plugins/kanban/dashboard/plugin_api.py`),
 * reused as-is via the desktop's namespace-scoped REST door. No new backend.
 *
 * Fetching, caching, polling, dedupe, and invalidation are React Query's job
 * (the app's standard, via the SDK). This module owns the query keys, the REST
 * calls, and the selected-board atom — every call passes `?board=<slug>` so the
 * desktop's selection never flips the server-wide current-board pointer.
 */

import { atom, type PluginRestOptions, type PluginStorage, queryClient } from '@hermes/plugin-sdk'

import type {
  BoardMeta,
  BoardsResponse,
  KanbanBoard,
  KanbanProfile,
  KanbanProject,
  KanbanTask,
  KanbanTaskDetail,
  OrchestrationSettings,
  TaskEstimate,
  WorkerLog
} from './types'

type Rest = <T>(path: string, opts?: PluginRestOptions) => Promise<T>
type Socket = (path: string, onMessage: (data: unknown) => void) => () => void

let rest: null | Rest = null

/** Selected board slug ('' = the server's current board). Persisted. */
export const $boardSlug = atom<string>('')

/** Whether the "how this board works" intro was dismissed. Persisted. */
export const $introDismissed = atom<boolean>(false)

/** Sub-group the Running lane by assignee (the dashboard's "lanes by
 *  profile"). Persisted. */
export const $lanesByProfile = atom<boolean>(false)

/** Per-lane collapse OVERRIDES (true=collapsed, false=expanded). Absence means
 *  auto: empty lanes collapse to a rail, occupied lanes expand. Persisted. */
export const $collapsedLanes = atom<Record<string, boolean>>({})

/** Global profile-description writes are not board-scoped. Keep their owner
 * outside any panel so a board switch/remount cannot start a second write while
 * the first request is still in flight. */
export const $profileDescriptionWriteOwner = atom<null | symbol>(null)

const BOARD_SLUG_KEY = 'boardSlug'
const INTRO_KEY = 'introDismissed'
const LANES_KEY = 'lanesByProfile'
const COLLAPSED_KEY = 'collapsedLanes'

/** One live `task_events` frame → precise cache invalidation: the board, plus
 *  each touched task's detail. The polls (8s board / 4s drawer) stay as the
 *  fallback — the socket just makes the board feel instant. */
function onEventsFrame(slug: string, data: unknown): void {
  const events = (data as { events?: Array<{ task_id?: string }> })?.events

  if (!events?.length) {
    return
  }

  void queryClient.invalidateQueries({ queryKey: ['kanban', 'board', slug] })
  // Any event can change a board's card count — keep the switcher badge honest.
  void queryClient.invalidateQueries({ queryKey: BOARDS_KEY })

  for (const taskId of new Set(events.map(event => event.task_id).filter(Boolean))) {
    void queryClient.invalidateQueries({ queryKey: taskKey(slug, taskId!) })
  }
}

// A persisted, subscribable atom (the structural slice we need — avoids
// importing nanostore's type just to describe one).
interface Persisted<T> {
  get(): T
  set(value: T): void
  listen(cb: (value: T) => void): () => void
}

/** Bind the plugin's doors at register time and return a disposer the host
 *  runs on unload/disable — so nothing (store sync, socket) survives a toggle
 *  or duplicates on re-enable. The events socket is pinned to a board at
 *  handshake, so a board switch closes + reopens it. */
export function bindApi(r: Rest, storage: PluginStorage, socket: Socket): () => void {
  rest = r
  const unsubs: Array<() => void> = []

  // Hydrate an atom from storage and keep storage in sync with it.
  const persist = <T>(atom: Persisted<T>, key: string, fallback: T) => {
    atom.set(storage.get(key, fallback))
    unsubs.push(atom.listen(value => storage.set(key, value)))
  }

  persist($boardSlug, BOARD_SLUG_KEY, '')
  persist($introDismissed, INTRO_KEY, false)
  persist($lanesByProfile, LANES_KEY, false)
  persist($collapsedLanes, COLLAPSED_KEY, {})

  let close: (() => void) | null = null

  const open = (slug: string) => {
    close?.()
    close = socket(slug ? `/events?board=${encodeURIComponent(slug)}` : '/events', data => onEventsFrame(slug, data))
  }

  open($boardSlug.get())
  unsubs.push($boardSlug.listen(open))

  return () => {
    unsubs.forEach(unsub => unsub())
    close?.()
    nudgeTimers.forEach(timer => clearTimeout(timer))
    nudgeTimers.clear()
    rest = null
  }
}

function call<T>(path: string, opts?: PluginRestOptions): Promise<T> {
  return rest ? rest<T>(path, opts) : Promise.reject(new Error('kanban api not ready'))
}

/** Append one explicitly captured board (and other params) to a path. */
function withBoard(slug: string, path: string, params: Record<string, string> = {}): string {
  const search = new URLSearchParams(params)

  if (slug) {
    search.set('board', slug)
  }

  const qs = search.toString()

  return qs ? `${path}?${qs}` : path
}

// ── query keys (all board-scoped so switching boards is a clean cache miss) ──

export const boardKey = (slug: string, archived: boolean) => ['kanban', 'board', slug, archived] as const
export const taskKey = (slug: string, id: string) => ['kanban', 'task', slug, id] as const
export const logKey = (slug: string, id: string) => ['kanban', 'log', slug, id] as const
export const BOARDS_KEY = ['kanban', 'boards'] as const
export const PROFILES_KEY = ['kanban', 'profiles'] as const
export const PROJECTS_KEY = ['kanban', 'projects'] as const
export const ORCHESTRATION_KEY = ['kanban', 'orchestration'] as const
export const ORCHESTRATION_MUTATION_KEY = ['kanban', 'orchestration-write'] as const
export const ORCHESTRATION_MUTATION_SCOPE = { id: 'kanban:orchestration-write' } as const
export const PROFILE_DESCRIPTION_MUTATION_KEY = ['kanban', 'profile-description-write'] as const
export const PROFILE_DESCRIPTION_MUTATION_SCOPE = { id: 'kanban:profile-description-write' } as const
export const profilesKey = (slug: string) => [...PROFILES_KEY, slug] as const
export const orchestrationKey = (slug: string) => [...ORCHESTRATION_KEY, slug] as const

// ── reads ─────────────────────────────────────────────────────────────────────

export const fetchBoard = (slug: string, archived = false): Promise<KanbanBoard> =>
  call<KanbanBoard>(withBoard(slug, '/board', archived ? { include_archived: 'true' } : {}))

export const fetchTask = (slug: string, id: string) => call<KanbanTaskDetail>(withBoard(slug, `/tasks/${id}`))

/** Worker stdout/stderr tail (last 16 KiB — plenty for the drawer). */
export const fetchLog = (slug: string, id: string) =>
  call<WorkerLog>(withBoard(slug, `/tasks/${id}/log`, { tail: '16384' }))

export const fetchBoards = () => call<BoardsResponse>('/boards')

export const fetchProfiles = (slug: string) => call<{ profiles: KanbanProfile[] }>(withBoard(slug, '/profiles'))

/** Shared query contract for every board-scoped assignment roster surface. */
export const profileQueryOptions = (slug: string) => ({
  queryFn: () => fetchProfiles(slug),
  queryKey: profilesKey(slug),
  staleTime: 60_000
})

/** First-class Hermes projects, for scoping a board's default workspace. */
export const fetchProjects = () => call<{ projects: KanbanProject[] }>('/projects')

export const fetchOrchestration = (slug: string) => call<OrchestrationSettings>(withBoard(slug, '/orchestration'))

// ── writes ────────────────────────────────────────────────────────────────────

/** Claim the one global profile-description write slot synchronously, before a
 * React Query mutation can be queued. The matching owner token prevents an old
 * completion from releasing a newer claim. */
export function claimProfileDescriptionWrite(): null | symbol {
  if ($profileDescriptionWriteOwner.get()) {
    return null
  }

  const owner = Symbol('kanban-profile-description-write')
  $profileDescriptionWriteOwner.set(owner)

  return owner
}

export function runProfileDescriptionWrite<T>(owner: symbol, write: () => Promise<T>): Promise<T> {
  if ($profileDescriptionWriteOwner.get() !== owner) {
    return Promise.reject(new Error('profile description write is already in progress'))
  }

  let request: Promise<T>

  try {
    request = write()
  } catch (error) {
    if ($profileDescriptionWriteOwner.get() === owner) {
      $profileDescriptionWriteOwner.set(null)
    }

    return Promise.reject(error)
  }

  return request.finally(() => {
    if ($profileDescriptionWriteOwner.get() === owner) {
      $profileDescriptionWriteOwner.set(null)
    }
  })
}

// Every board edit nudges the dispatcher (debounced, fire-and-forget) so the
// change takes effect NOW instead of on the next 60s tick — create a ready
// task and the worker spawns immediately, no manual "nudge" ritual. The tick
// is lock-guarded and ~1ms when there's nothing to do, so over-nudging is
// free; failures are non-events (the periodic tick still exists).
const nudgeTimers = new Map<string, ReturnType<typeof setTimeout>>()

function autoNudge(slug: string): void {
  const pending = nudgeTimers.get(slug)

  if (pending != null) {
    clearTimeout(pending)
  }

  nudgeTimers.set(
    slug,
    setTimeout(() => {
      nudgeTimers.delete(slug)
      nudgeDispatcher(slug).catch(() => undefined)
    }, 400)
  )
}

/** Resolve the write, then kick the dispatcher. Rejections pass through. */
function nudged<T>(slug: string, write: Promise<T>): Promise<T> {
  return write.then(value => {
    autoNudge(slug)

    return value
  })
}

export const patchTask = (slug: string, id: string, patch: Record<string, unknown>) =>
  nudged(slug, call(withBoard(slug, `/tasks/${id}`), { method: 'PATCH', body: patch }))

export const createTask = (slug: string, body: Record<string, unknown>) =>
  nudged(slug, call<{ task: KanbanTask | null; warning?: string }>(withBoard(slug, '/tasks'), { method: 'POST', body }))

// Deleting can unblock dependants (a gone parent no longer gates), so it
// nudges too.
export const deleteTask = (slug: string, id: string) =>
  nudged(slug, call(withBoard(slug, `/tasks/${id}`), { method: 'DELETE' }))

/** One patch, many ids — independent per-id application; returns per-id
 *  outcomes so the UI can toast partial failures. */
export const bulkTasks = (slug: string, ids: string[], patch: Record<string, unknown>) =>
  nudged(
    slug,
    call<{ results: Array<{ id: string; ok: boolean; error?: string }> }>(withBoard(slug, '/tasks/bulk'), {
      method: 'POST',
      body: { ids, ...patch }
    })
  )

export const addComment = (slug: string, id: string, body: string) =>
  call(withBoard(slug, `/tasks/${id}/comments`), { method: 'POST', body: { author: 'desktop', body } })

export const assignTask = (slug: string, id: string, profile: null | string) =>
  nudged(
    slug,
    call(withBoard(slug, `/tasks/${id}/reassign`), { method: 'POST', body: { profile, reclaim_first: true } })
  )

export const reclaimTask = (slug: string, id: string) =>
  nudged(slug, call(withBoard(slug, `/tasks/${id}/reclaim`), { method: 'POST', body: {} }))

export const uploadAttachment = (
  slug: string,
  id: string,
  upload: { filename: string; contentType?: string; bytes: ArrayBuffer }
) => call(withBoard(slug, `/tasks/${id}/attachments`), { method: 'POST', upload })

export const createBoard = (slug: string, name: string, projectId?: string) =>
  call<{ board: { slug: string } }>('/boards', {
    method: 'POST',
    body: { slug, name, ...(projectId ? { project_id: projectId } : {}) }
  })

/** Rough auxiliary-model estimate for a task (tokens + complexity). Makes a
 *  model call — gate behind an explicit user action + disclaimer. */
export const estimateTask = (slug: string, id: string) =>
  call<TaskEstimate>(withBoard(slug, `/tasks/${id}/estimate`), { method: 'POST', body: {} })

/** Estimate from typed title/body before a task exists (create dialog). */
export const estimateNew = (title: string, body: string) =>
  call<TaskEstimate>('/estimate', { method: 'POST', body: { title, body: body || undefined } })

/** Edit a board's display metadata + default project directory. Pass
 *  `default_workdir: ''` to clear it. Slug is immutable. */
export const updateBoard = (slug: string, patch: Record<string, unknown>) =>
  call<{ board: BoardMeta }>(`/boards/${encodeURIComponent(slug)}`, { method: 'PATCH', body: patch })

export const nudgeDispatcher = (slug: string) =>
  call<{ spawned?: unknown[] }>(withBoard(slug, '/dispatch'), { method: 'POST', body: {} })

export const saveOrchestration = (slug: string, patch: Record<string, unknown>) =>
  call<OrchestrationSettings>(withBoard(slug, '/orchestration'), { method: 'PUT', body: patch })

export const saveProfileDescription = (name: string, description: string) =>
  call(`/profiles/${encodeURIComponent(name)}`, { method: 'PATCH', body: { description } })

export const autoDescribeProfile = (name: string) =>
  call<{ ok: boolean; reason?: null | string; description?: null | string }>(
    `/profiles/${encodeURIComponent(name)}/describe-auto`,
    { method: 'POST', body: { overwrite: true } }
  )
