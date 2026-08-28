import { type CronJob, type CronOwner, getApiRequestConnection, getCronJobs, triggerCronJob } from '@/hermes'
import {
  beginCronJobsAction,
  beginCronJobsRequest,
  commitCronJobsRequest,
  type CronJobsRequest,
  isCronJobsRequestCurrent,
  isCronJobsScopeCurrent
} from '@/store/cron'

export interface CronTriggerRefreshResult {
  jobs: CronJob[] | null
  refreshError: unknown | null
  stale: boolean
}

export interface CronMutationRefreshResult<T> extends CronTriggerRefreshResult {
  value: T | null
}

function cronRequestScope(profile: string, connectionId: null | string): string {
  return `${connectionId ?? ''}\u0000${profile}`
}

async function refreshForGeneration(
  profile: string,
  connectionId: null | string,
  request: CronJobsRequest
): Promise<CronTriggerRefreshResult> {
  try {
    const jobs = await getCronJobs({ connectionId, profile })

    if (!commitCronJobsRequest(request, jobs)) {
      return { jobs: null, refreshError: null, stale: true }
    }

    return { jobs, refreshError: null, stale: false }
  } catch (refreshError) {
    if (!isCronJobsRequestCurrent(request)) {
      return { jobs: null, refreshError: null, stale: true }
    }

    return { jobs: null, refreshError, stale: false }
  }
}

export function refreshCronJobs(profile: string): Promise<CronTriggerRefreshResult> {
  const connectionId = getApiRequestConnection()
  return refreshForGeneration(profile, connectionId, beginCronJobsRequest(cronRequestScope(profile, connectionId)))
}

export async function mutateAndRefreshCronJobs<T>(
  profile: string,
  mutate: () => Promise<T>
): Promise<CronMutationRefreshResult<T>> {
  const connectionId = getApiRequestConnection()
  const scopeToken = beginCronJobsAction(cronRequestScope(profile, connectionId))
  let value: T

  try {
    value = await mutate()
  } catch (mutationError) {
    if (!isCronJobsScopeCurrent(scopeToken)) {
      return { jobs: null, refreshError: null, stale: true, value: null }
    }

    throw mutationError
  }

  if (!isCronJobsScopeCurrent(scopeToken)) {
    return { jobs: null, refreshError: null, stale: true, value: null }
  }

  const refreshed = await refreshForGeneration(
    profile,
    connectionId,
    beginCronJobsRequest(cronRequestScope(profile, connectionId))
  )

  if (!isCronJobsScopeCurrent(scopeToken)) {
    return { jobs: null, refreshError: null, stale: true, value: null }
  }

  // A newer request in the same scope may supersede this refresh after the
  // mutation itself has already succeeded. Preserve the mutation result so
  // callers can settle dialogs/toasts without publishing the older snapshot.
  if (refreshed.stale) {
    return { jobs: null, refreshError: null, stale: false, value }
  }

  return { ...refreshed, value }
}

/**
 * Trigger a job synchronously, then replace the local view from the backend.
 * A completed one-shot may have been deleted, so the trigger response alone is
 * not an authoritative list update. Refresh failure is reported separately:
 * the trigger already succeeded and must not be shown as failed.
 */
export async function triggerAndRefreshCronJobs(
  jobId: string,
  profile: 'all' | string,
  owner?: CronOwner
): Promise<CronTriggerRefreshResult> {
  const resolvedOwner = owner ?? (profile === 'all' ? undefined : { connectionId: getApiRequestConnection(), profile })
  const { value: _value, ...result } = await mutateAndRefreshCronJobs(profile, () =>
    triggerCronJob(jobId, resolvedOwner)
  )

  return result
}
