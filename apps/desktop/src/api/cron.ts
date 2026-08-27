import type {
  AutomationBlueprint,
  CronDeliveryTarget,
  CronJob,
  CronJobCreatePayload,
  CronJobOutput,
  CronJobOutputDetail,
  CronJobUpdates,
  SessionInfo
} from '@/types/hermes'

import {
  connectionScoped,
  getApiRequestConnection,
  hermesApi,
  profileScoped,
  STARTUP_REQUEST_TIMEOUT_MS
} from './client'

const CRON_TRIGGER_REQUEST_TIMEOUT_MS = 24 * 60 * 60 * 1000

export interface CronOwnerScope {
  connectionId?: null | string
  profile?: null | string
}

export type CronOwner = CronOwnerScope | string | undefined

/** Resolve once at call entry. Object owners are authoritative (including
 * explicit local/null); legacy strings retain the ambient gateway behavior. */
function cronOwner(owner?: CronOwner): {
  connectionId: null | string | undefined
  profile: string | undefined
  requestScope: { connectionId?: null | string; profile?: null | string }
} {
  if (owner && typeof owner === 'object') {
    const profile = owner.profile?.trim() || undefined
    const connectionId = owner.connectionId?.trim() || null

    return { connectionId, profile, requestScope: { connectionId, profile: profile ?? null } }
  }

  return {
    connectionId: getApiRequestConnection(),
    profile: owner?.trim() || undefined,
    requestScope: { ...profileScoped(), ...connectionScoped() }
  }
}

function ownerQuery(scope: ReturnType<typeof cronOwner>, separator: '?' | '&' = '?'): string {
  return scope.profile ? `${separator}profile=${encodeURIComponent(scope.profile)}` : ''
}

export async function getCronJobs(owner?: CronOwner): Promise<CronJob[]> {
  const scope = cronOwner(owner)
  const jobs = await hermesApi<CronJob[]>({
    ...scope.requestScope,
    path: `/api/cron/jobs${ownerQuery(scope)}`,
    timeoutMs: STARTUP_REQUEST_TIMEOUT_MS
  })

  // The backend row is profile-qualified but the connection is a Desktop
  // transport fact. Stamp it at the boundary so list identity and every later
  // focus/read/mutation remain pinned to the backend that returned the row.
  return Array.isArray(jobs) ? jobs.map(job => ({ ...job, connection_id: scope.connectionId ?? null })) : []
}

export function getCronJob(jobId: string, owner?: CronOwner): Promise<CronJob> {
  const scope = cronOwner(owner)
  return hermesApi<CronJob>({
    ...scope.requestScope,
    path: `/api/cron/jobs/${encodeURIComponent(jobId)}${ownerQuery(scope)}`
  })
}

export async function getCronJobRuns(jobId: string, limit = 20, owner?: CronOwner): Promise<SessionInfo[]> {
  const scope = cronOwner(owner)
  const { runs } = await hermesApi<{ runs: SessionInfo[] }>({
    ...scope.requestScope,
    path: `/api/cron/jobs/${encodeURIComponent(jobId)}/runs?limit=${limit}${ownerQuery(scope, '&')}`
  })

  return runs ?? []
}

export async function getCronJobOutputs(jobId: string, limit = 20, owner?: CronOwner): Promise<CronJobOutput[]> {
  const scope = cronOwner(owner)
  const { outputs } = await hermesApi<{ outputs: CronJobOutput[] }>({
    ...scope.requestScope,
    path: `/api/cron/jobs/${encodeURIComponent(jobId)}/outputs?limit=${limit}${ownerQuery(scope, '&')}`
  })

  return outputs ?? []
}

export function getCronJobOutput(jobId: string, outputId: string, owner?: CronOwner): Promise<CronJobOutputDetail> {
  const scope = cronOwner(owner)
  return hermesApi<CronJobOutputDetail>({
    ...scope.requestScope,
    path: `/api/cron/jobs/${encodeURIComponent(jobId)}/outputs/${encodeURIComponent(outputId)}${ownerQuery(scope)}`
  })
}

export async function getCronDeliveryTargets(): Promise<CronDeliveryTarget[]> {
  const { targets } = await hermesApi<{ targets: CronDeliveryTarget[] }>({
    ...profileScoped(),
    ...connectionScoped(),
    path: '/api/cron/delivery-targets'
  })

  return targets ?? []
}

export function createCronJob(body: CronJobCreatePayload, owner?: CronOwner): Promise<CronJob> {
  const scope = cronOwner(owner)
  return hermesApi<CronJob>({ ...scope.requestScope, path: `/api/cron/jobs${ownerQuery(scope)}`, method: 'POST', body })
}

export function updateCronJob(jobId: string, updates: CronJobUpdates, owner?: CronOwner): Promise<CronJob> {
  const scope = cronOwner(owner)
  return hermesApi<CronJob>({
    ...scope.requestScope,
    path: `/api/cron/jobs/${encodeURIComponent(jobId)}${ownerQuery(scope)}`,
    method: 'PUT',
    body: { updates }
  })
}

export function pauseCronJob(jobId: string, owner?: CronOwner): Promise<CronJob> {
  const scope = cronOwner(owner)
  return hermesApi<CronJob>({
    ...scope.requestScope,
    path: `/api/cron/jobs/${encodeURIComponent(jobId)}/pause${ownerQuery(scope)}`,
    method: 'POST'
  })
}

export function resumeCronJob(jobId: string, owner?: CronOwner): Promise<CronJob> {
  const scope = cronOwner(owner)
  return hermesApi<CronJob>({
    ...scope.requestScope,
    path: `/api/cron/jobs/${encodeURIComponent(jobId)}/resume${ownerQuery(scope)}`,
    method: 'POST'
  })
}

export function triggerCronJob(jobId: string, owner?: CronOwner): Promise<CronJob> {
  const scope = cronOwner(owner)
  return hermesApi<CronJob>({
    ...scope.requestScope,
    path: `/api/cron/jobs/${encodeURIComponent(jobId)}/trigger${ownerQuery(scope)}`,
    method: 'POST',
    timeoutMs: CRON_TRIGGER_REQUEST_TIMEOUT_MS
  })
}

export function deleteCronJob(jobId: string, owner?: CronOwner): Promise<{ ok: boolean }> {
  const scope = cronOwner(owner)
  return hermesApi<{ ok: boolean }>({
    ...scope.requestScope,
    path: `/api/cron/jobs/${encodeURIComponent(jobId)}${ownerQuery(scope)}`,
    method: 'DELETE'
  })
}

export function getAutomationBlueprints(): Promise<{ blueprints: AutomationBlueprint[] }> {
  return hermesApi<{ blueprints: AutomationBlueprint[] }>({
    ...profileScoped(),
    ...connectionScoped(),
    path: '/api/cron/blueprints',
    timeoutMs: STARTUP_REQUEST_TIMEOUT_MS
  })
}

export function instantiateAutomationBlueprint(
  body: { blueprint: string; values: Record<string, string> },
  profile: string
): Promise<CronJob> {
  return hermesApi<CronJob>({
    ...profileScoped(),
    ...connectionScoped(),
    path: `/api/cron/blueprints/instantiate?profile=${encodeURIComponent(profile)}`,
    method: 'POST',
    body
  })
}
