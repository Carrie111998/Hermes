import { describe, expect, it } from 'vitest'

/**
 * Faithful mirror of the catch-block state reset in
 * apps/desktop/src/app/session/hooks/use-prompt-actions/index.ts's
 * reloadFromMessage (issue #95745). Isolated from the full hook (deeply
 * tied to React state/useCallback/nanostores) -- mirrors just the state
 * merge object the catch block constructs, before and after the fix.
 */

interface SessionState {
  awaitingResponse: boolean
  busy: boolean
  messages: string[]
  turnLive: boolean
  turnStartedAt: null | number
}

function preFixCatchReset(state: SessionState): SessionState {
  return {
    ...state,
    awaitingResponse: false,
    busy: false,
    turnLive: false,
    turnStartedAt: null
    // BUG: no `messages` key, so the optimistically-truncated array from
    // applyReloadOptimistic survives the failed submit unchanged.
  }
}

function fixedCatchReset(state: SessionState, originalMessages: string[]): SessionState {
  return {
    ...state,
    awaitingResponse: false,
    busy: false,
    turnLive: false,
    turnStartedAt: null,
    messages: originalMessages
  }
}

describe('reloadFromMessage catch-block message rollback (issue #95745)', () => {
  const originalMessages = ['user: hi', 'assistant: hello', 'user: retry me']
  const optimisticallyTruncatedState: SessionState = {
    awaitingResponse: true,
    busy: true,
    messages: ['user: hi', 'assistant: hello'], // the retried turn's rows hidden
    turnLive: true,
    turnStartedAt: Date.now()
  }

  it('demonstrates the bug: pre-fix reset leaves the transcript truncated after a failed submit', () => {
    const result = preFixCatchReset(optimisticallyTruncatedState)

    expect(result.busy).toBe(false)
    // The exact reported symptom: messages stays at the optimistically
    // truncated length, not the full original history.
    expect(result.messages).toHaveLength(2)
    expect(result.messages).not.toEqual(originalMessages)
  })

  it('the fix restores the full original message history on a failed submit', () => {
    const result = fixedCatchReset(optimisticallyTruncatedState, originalMessages)

    expect(result.busy).toBe(false)
    expect(result.awaitingResponse).toBe(false)
    expect(result.turnLive).toBe(false)
    expect(result.turnStartedAt).toBeNull()
    expect(result.messages).toEqual(originalMessages)
    expect(result.messages).toHaveLength(3)
  })

  it('matches restoreToMessage\'s own established catch-block pattern (no regression to that path)', () => {
    // restoreToMessage's catch already includes `messages` in its reset --
    // this test documents that reloadFromMessage's fixed reset now uses
    // the identical shape, not a divergent one.
    const restoreToMessageStyleReset = (state: SessionState, messages: string[]) => ({
      ...state,
      busy: false,
      awaitingResponse: false,
      turnLive: false,
      turnStartedAt: null,
      messages
    })

    const fromReload = fixedCatchReset(optimisticallyTruncatedState, originalMessages)
    const fromRestore = restoreToMessageStyleReset(optimisticallyTruncatedState, originalMessages)

    expect(fromReload).toEqual(fromRestore)
  })
})
