import { atom } from 'nanostores'

import { $gateway } from './gateway'

/** Rebuildable desktop projection of the canonical GoalManager/SessionDB row. */
export interface SessionGoalProjection {
  checkpointRevision: number
  continuationPending: boolean
  goal: string
  goalId: string
  lastStopReason?: string | null
  maxTurns: number
  nextAction?: string | null
  outcome: string
  status: string
  turnsUsed: number
  updatedAt: number
}

interface GoalStatusResponse {
  checkpoint_revision?: number
  continuation_pending?: boolean
  exists: boolean
  goal?: string
  goal_id?: string
  last_stop_reason?: string | null
  max_turns?: number
  next_action?: string | null
  outcome?: string
  status?: string
  turns_used?: number
  updated_at?: number
}

export const $goalsBySession = atom<Record<string, SessionGoalProjection>>({})

export function clearSessionGoal(sessionId: string): void {
  const current = $goalsBySession.get()
  if (!(sessionId in current)) {
    return
  }
  const { [sessionId]: _removed, ...rest } = current
  $goalsBySession.set(rest)
}

/**
 * Refresh a UI-only cache from the typed read-only goal.status RPC.
 * It never parses transcript/status prose and never writes canonical goal state.
 */
export async function refreshSessionGoal(sessionId: string): Promise<void> {
  const gateway = $gateway.get()
  if (!gateway || !sessionId) {
    return
  }

  const projection = await gateway.request<GoalStatusResponse>('goal.status', { session_id: sessionId })
  if (!projection?.exists) {
    clearSessionGoal(sessionId)
    return
  }
  if (!projection.goal || !projection.goal_id || !projection.status || !projection.outcome) {
    return
  }

  $goalsBySession.set({
    ...$goalsBySession.get(),
    [sessionId]: {
      goalId: projection.goal_id,
      goal: projection.goal,
      status: projection.status,
      outcome: projection.outcome,
      turnsUsed: projection.turns_used ?? 0,
      maxTurns: projection.max_turns ?? 0,
      nextAction: projection.next_action,
      lastStopReason: projection.last_stop_reason,
      continuationPending: Boolean(projection.continuation_pending),
      checkpointRevision: projection.checkpoint_revision ?? 0,
      updatedAt: projection.updated_at ?? 0
    }
  })
}
