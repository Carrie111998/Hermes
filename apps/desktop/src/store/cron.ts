import { atom } from 'nanostores'

import { cronJobVisibilityKey } from '@/lib/cron-session-visibility'
import { Codecs, persistentAtom } from '@/lib/persisted'
import type { CronJob } from '@/types/hermes'

const HIDDEN_CRON_JOBS_STORAGE_KEY = 'hermes.desktop.cronJobsHiddenFromSessions.v1'

// Cron *jobs* (not run sessions) power the sidebar "Cron jobs" section. Listing
// the job — schedule, state, live next-run countdown — makes the job the
// first-class entity; its runs (sessions) resolve under it in the cron detail.
export const $cronJobs = atom<CronJob[]>([])
export const setCronJobs = (jobs: CronJob[]) => $cronJobs.set(jobs)

// In-place edit so the cron overlay's mutations (create/edit/delete/pause/…)
// land in the same atom the sidebar renders — no stale list until the next poll.
export const updateCronJobs = (fn: (jobs: CronJob[]) => CronJob[]) => $cronJobs.set(fn($cronJobs.get()))

// Presentation opt-outs: absence means every cron job is visible in Sessions.
// Each key is JSON [normalizedProfile, jobId], so profiles may reuse job ids.
export const $cronJobsHiddenFromSessions = persistentAtom<string[]>(
  HIDDEN_CRON_JOBS_STORAGE_KEY,
  [],
  Codecs.stringArray
)

export function setCronJobInSessions(jobId: string, profile: null | string | undefined, shown: boolean): void {
  const key = cronJobVisibilityKey(jobId, profile)
  const current = $cronJobsHiddenFromSessions.get()
  const hidden = current.includes(key)

  if (shown === !hidden) {
    return
  }

  $cronJobsHiddenFromSessions.set(shown ? current.filter(item => item !== key) : [...current, key])
}

// One-shot focus target: clicking "Manage" on a job sets this, then opens the
// cron overlay, which reads it once to select + scroll to that job. Cleared
// after consumption so re-opening cron normally doesn't re-focus a stale job.
export interface CronJobIdentity {
  id: string
  profile?: null | string
}

export const $cronFocusJob = atom<CronJobIdentity | null>(null)
export const setCronFocusJob = (job: CronJobIdentity | null) => $cronFocusJob.set(job)
