import { afterEach, describe, expect, it, vi } from 'vitest'

import { $gateway } from './gateway'
import { $goalsBySession, clearSessionGoal, refreshSessionGoal } from './goals'

describe('goal projection store', () => {
  afterEach(() => {
    $gateway.set(null)
    $goalsBySession.set({})
    vi.restoreAllMocks()
  })

  it('hydrates only from the typed goal.status projection', async () => {
    const request = vi.fn(async () => ({
      exists: true,
      goal: 'ship typed projection',
      goal_id: 'goal-1',
      status: 'active',
      outcome: 'CONTINUATION_REQUIRED',
      turns_used: 3,
      max_turns: 20,
      next_action: 'continue',
      last_stop_reason: 'provider failure',
      continuation_pending: true,
      checkpoint_revision: 4,
      updated_at: 123
    }))
    $gateway.set({ request } as never)

    await refreshSessionGoal('runtime-session')

    expect(request).toHaveBeenCalledWith('goal.status', { session_id: 'runtime-session' })
    expect($goalsBySession.get()['runtime-session']).toEqual({
      goalId: 'goal-1',
      goal: 'ship typed projection',
      status: 'active',
      outcome: 'CONTINUATION_REQUIRED',
      turnsUsed: 3,
      maxTurns: 20,
      nextAction: 'continue',
      lastStopReason: 'provider failure',
      continuationPending: true,
      checkpointRevision: 4,
      updatedAt: 123
    })
  })

  it('clears the rebuildable projection when canonical state does not exist', async () => {
    $goalsBySession.set({ s1: { goalId: 'old', goal: 'old', status: 'paused', outcome: 'GOAL_PAUSED', turnsUsed: 1, maxTurns: 20, continuationPending: false, checkpointRevision: 1, updatedAt: 1 } })
    $gateway.set({ request: vi.fn(async () => ({ exists: false })) } as never)

    await refreshSessionGoal('s1')

    expect($goalsBySession.get().s1).toBeUndefined()
    clearSessionGoal('s1')
  })
})
