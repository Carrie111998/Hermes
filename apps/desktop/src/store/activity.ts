import { atom } from 'nanostores'

import { type ProfileScope, profileScopeCacheKey } from '@/api/client'
import { sessionTitle } from '@/lib/chat-runtime'
import type { PreviewServerRestart } from '@/store/preview'
import type { ActionStatusResponse, SessionInfo } from '@/types/hermes'

const HISTORY_LIMIT = 8
const COMPLETED_TTL_MS = 5 * 60 * 1000

export type RailTaskStatus = 'error' | 'running' | 'success'

export interface RailTask {
  id: string
  label: string
  detail: string
  status: RailTaskStatus
  updatedAt: number
}

export interface DesktopActionTask {
  status: ActionStatusResponse
  updatedAt: number
}

export const $desktopActionTasks = atom<Record<string, DesktopActionTask>>({})

export function desktopActionTaskKey(name: string, scope?: ProfileScope): string {
  return `${profileScopeCacheKey(scope)}:${name}`
}

export function upsertDesktopActionTask(status: ActionStatusResponse, scope?: ProfileScope): void {
  const key = desktopActionTaskKey(status.name, scope)

  $desktopActionTasks.set(prune({ ...$desktopActionTasks.get(), [key]: { status, updatedAt: Date.now() } }))
}

export function buildRailTasks(
  workingSessionIds: readonly string[],
  sessions: readonly SessionInfo[],
  previewRestart: PreviewServerRestart | null,
  actionTasks: Record<string, DesktopActionTask>
): RailTask[] {
  const sessionsById = new Map(sessions.map(session => [session.id, session]))

  const sessionTasks: RailTask[] = workingSessionIds.map((id, index) => {
    const session = sessionsById.get(id)

    return {
      id: `session:${id}`,
      label: session ? sessionTitle(session) : 'Session task',
      detail: 'Agent task running',
      status: 'running',
      updatedAt: session?.last_active || Date.now() - index
    }
  })

  const previewTasks: RailTask[] = previewRestart
    ? [
        {
          id: `preview:${previewRestart.taskId}`,
          label: 'Preview restart',
          detail: previewRestart.message || previewRestart.url,
          status:
            previewRestart.status === 'error' ? 'error' : previewRestart.status === 'running' ? 'running' : 'success',
          updatedAt: Date.now()
        }
      ]
    : []

  const actions: RailTask[] = Object.entries(actionTasks).map(([key, { status, updatedAt }]) => ({
    id: `action:${key}`,
    label: status.name,
    detail: actionDetail(status),
    status: actionStatus(status),
    updatedAt
  }))

  return [...sessionTasks, ...previewTasks, ...actions].sort((left, right) => right.updatedAt - left.updatedAt)
}

function actionStatus(status: ActionStatusResponse): RailTaskStatus {
  if (status.running) {
    return 'running'
  }

  return status.exit_code === 0 ? 'success' : 'error'
}

function actionDetail(status: ActionStatusResponse): string {
  if (status.running) {
    return 'Running'
  }

  return status.exit_code === 0 ? 'Completed' : `Failed (${status.exit_code ?? 'unknown'})`
}

function prune(tasks: Record<string, DesktopActionTask>): Record<string, DesktopActionTask> {
  const now = Date.now()

  return Object.fromEntries(
    Object.entries(tasks)
      .filter(([, task]) => now - task.updatedAt <= COMPLETED_TTL_MS)
      .sort(([, left], [, right]) => right.updatedAt - left.updatedAt)
      .slice(0, HISTORY_LIMIT)
  )
}
