import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { turnController } from '../app/turnController.js'
import { resetTurnState } from '../app/turnStore.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'

// Live context-usage projection: the bottom context meter should tick up
// during streaming (not just at turn end) so it matches the detail panel
// instead of looking frozen until message.complete arrives.
describe('turnController live usage projection', () => {
  beforeEach(() => {
    resetUiState()
    resetTurnState()
    turnController.fullReset()
    // Simulate an authoritative baseline from a prior turn.
    patchUiState({
      busy: false,
      usage: { available: true, context_max: 200_000, context_used: 50_000, context_percent: 25 }
    })
  })

  afterEach(() => {
    turnController.fullReset()
    resetUiState()
    resetTurnState()
  })

  it('projects rising context_used as the reply streams in', () => {
    turnController.startMessage() // captures baseline 50_000
    // 4 chars ~= 1 token under the rough estimator; 4000 chars ~= 1000 tokens.
    turnController.recordMessageDelta({ text: 'x'.repeat(4000) })

    const u = getUiState().usage!
    expect(u.context_used).toBeGreaterThan(50_000)
    // 50_000 + ~1000 = 51_000 → ~25.5% of 200_000
    expect(u.context_percent).toBeGreaterThan(25)
    expect(u.context_percent).toBeLessThanOrEqual(100)
  })

  it('does not project when context_max is unknown', () => {
    patchUiState({ usage: { available: false } })
    turnController.startMessage()
    turnController.recordMessageDelta({ text: 'x'.repeat(4000) })
    // No context_max → publishLiveUsage bails; usage stays untouched.
    expect(getUiState().usage?.context_used).toBeUndefined()
  })

  it('lets the authoritative server value overwrite the projection at turn end', () => {
    turnController.startMessage()
    turnController.recordMessageDelta({ text: 'x'.repeat(4000) })
    const projected = getUiState().usage!.context_used
    expect(projected).toBeGreaterThan(50_000)

    // In the real app, the message.complete event handler (createGatewayEventHandler)
    // merges the server's authoritative usage over the live projection:
    //   patchUiState(state => ({ ...state, usage: { ...state.usage, ...ev.payload.usage } }))
    turnController.recordMessageComplete({ text: 'done' })
    patchUiState(state => ({
      ...state,
      usage: { ...state.usage, context_max: 200_000, context_used: 120_000, context_percent: 60 }
    }))
    const u = getUiState().usage!
    expect(u.context_used).toBe(120_000)
    expect(u.context_percent).toBe(60)
  })
})
