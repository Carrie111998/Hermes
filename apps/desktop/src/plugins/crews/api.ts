/**
 * Crews data layer. Everything goes through `ctx.rest` — the plugin's own
 * `/api/plugins/crews/*` FastAPI router (plugins/crews/dashboard/plugin_api.py),
 * reused as-is via the desktop's namespace-scoped REST door.
 *
 * Fetching, caching, polling, dedupe and invalidation are React Query's job
 * (the app's standard, via the SDK). This module owns the query keys, the
 * REST calls, the selected-crew atom, and the live /events socket binding.
 */
import { atom, type PluginRestOptions, type PluginStorage, queryClient, useValue } from '@hermes/plugin-sdk'

import type { Crew, CrewEvent, CrewTemplate, Persona, Workflow, WorkflowRun } from './types'

type Rest = <T>(path: string, opts?: PluginRestOptions) => Promise<T>
type Socket = (path: string, onMessage: (data: unknown) => void) => () => void

let rest: null | Rest = null

/** Selected crew id — '' means the list view. Persisted. */
export const $selectedCrewId = atom<string>('')
/** Whether the create-crew dialog is open. */
export const $createOpen = atom<boolean>(false)
/** Template chosen to pre-fill the create dialog ('' = none). */
export const $templateId = atom<string>('')
/** The live activity feed — last N events for the selected crew. */
export const $activityFeed = atom<CrewEvent[]>([])
/** Task status map for the currently running workflow (taskId → status). */
export const $workflowStatus = atom<Record<string, string>>({})
/** Current workflow run id (when a run is in flight). */
export const $activeRunId = atom<string | null>(null)

const SELECTED_KEY = 'selectedCrewId'

const MAX_FEED = 200

function pushFeed(event: CrewEvent): void {
  const feed = $activityFeed.get()
  $activityFeed.set([...feed, event].slice(-MAX_FEED))
}

/** Bind the plugin's doors at register time and return a disposer. */
export function bindApi(r: Rest, storage: PluginStorage, socket: Socket): () => void {
  rest = r
  const unsubs: Array<() => void> = []

  $selectedCrewId.set(storage.get(SELECTED_KEY, ''))
  unsubs.push($selectedCrewId.listen(value => storage.set(SELECTED_KEY, value)))

  const close = socket('/events', data => onEvent(data as CrewEvent))

  return () => {
    unsubs.forEach(unsub => unsub())
    close()
    rest = null
  }
}

function onEvent(event: CrewEvent): void {
  if (!event || typeof event !== 'object' || !('type' in event)) {return}

  // Keep the live feed fresh (only for the crew on screen).
  const selected = $selectedCrewId.get()

  if ('crewId' in event && selected && event.crewId === selected) {
    pushFeed(event)
  }

  switch (event.type) {
    case 'crew_updated':

    case 'crew_deleted':
      void queryClient.invalidateQueries({ queryKey: CREWS_KEY })

      break

    case 'member_status':
      if ('crewId' in event) {
        void queryClient.invalidateQueries({ queryKey: crewKey(event.crewId) })
      }

      break

    case 'worker_end':

    case 'task_status':
      if ('crewId' in event) {
        void queryClient.invalidateQueries({ queryKey: crewKey(event.crewId) })
        void queryClient.invalidateQueries({ queryKey: workflowKey(event.crewId) })
        void queryClient.invalidateQueries({ queryKey: runsKey(event.crewId) })
      }

      break

    case 'run_started':

    case 'run_end':
      if ('crewId' in event) {
        void queryClient.invalidateQueries({ queryKey: crewKey(event.crewId) })
        void queryClient.invalidateQueries({ queryKey: runsKey(event.crewId) })
      }

      break

    default:
      break
  }
}

function call<T>(path: string, opts?: PluginRestOptions): Promise<T> {
  return rest ? rest<T>(path, opts) : Promise.reject(new Error('crews api not ready'))
}

// ── query keys ───────────────────────────────────────────────────────────────

export const CREWS_KEY = ['crews', 'list'] as const
export const PERSONAS_KEY = ['crews', 'personas'] as const
export const TEMPLATES_KEY = ['crews', 'templates'] as const
export const crewKey = (crewId: string) => ['crews', crewId] as const
export const workflowKey = (crewId: string) => ['crews', crewId, 'workflow'] as const
export const runsKey = (crewId: string) => ['crews', crewId, 'runs'] as const

// ── reads ────────────────────────────────────────────────────────────────────

export const fetchPersonas = () => call<{ ok: boolean; personas: Persona[] }>('/personas')
export const fetchTemplates = () => call<{ ok: boolean; templates: CrewTemplate[] }>('/templates')
export const fetchCrews = () => call<{ ok: boolean; crews: Crew[] }>('/crews')
export const fetchCrew = (crewId: string) => call<{ ok: boolean; crew: Crew }>(`/crews/${encodeURIComponent(crewId)}`)
export const fetchWorkflow = (crewId: string) =>
  call<{ ok: boolean; workflow: Workflow | null }>(`/crews/${encodeURIComponent(crewId)}/workflow`)
export const fetchRuns = (crewId: string) =>
  call<{ ok: boolean; runs: WorkflowRun[] }>(`/crews/${encodeURIComponent(crewId)}/runs`)

// ── writes ───────────────────────────────────────────────────────────────────

export interface CreateCrewInput {
  name: string
  goal: string
  members: Array<{ persona: string; role?: string; model?: string | null; profileName?: string | null }>
}

export const createCrew = (input: CreateCrewInput) =>
  call<{ ok: boolean; crew: Crew }>('/crews', { method: 'POST', body: input })

export const updateCrew = (crewId: string, patch: Record<string, unknown>) =>
  call<{ ok: boolean; crew: Crew }>(`/crews/${encodeURIComponent(crewId)}`, { method: 'PATCH', body: patch })

export const deleteCrew = (crewId: string) =>
  call<{ ok: boolean }>(`/crews/${encodeURIComponent(crewId)}`, { method: 'DELETE' })

export const cloneCrew = (crewId: string) =>
  call<{ ok: boolean; crew: Crew }>(`/crews/${encodeURIComponent(crewId)}/clone`, { method: 'POST', body: {} })

export const dispatchTask = (crewId: string, task: string, target: 'all' | string = 'all') =>
  call<{ ok: boolean; dispatched: string[] }>(`/crews/${encodeURIComponent(crewId)}/dispatch`, {
    method: 'POST',
    body: { task, target }
  })

export const saveWorkflow = (crewId: string, tasks: Workflow['tasks'], edges: Workflow['edges']) =>
  call<{ ok: boolean; workflow: Workflow }>(`/crews/${encodeURIComponent(crewId)}/workflow`, {
    method: 'PUT',
    body: { tasks, edges }
  })

export const runWorkflow = (crewId: string) =>
  call<{ ok: boolean; runId: string }>(`/crews/${encodeURIComponent(crewId)}/workflow/run`, {
    method: 'POST',
    body: {}
  })

/** Subscribe to the live feed atom (used by the detail screen). */
export function useActivityFeed(): CrewEvent[] {
  return useValue($activityFeed)
}
