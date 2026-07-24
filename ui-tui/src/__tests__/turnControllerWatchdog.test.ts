import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { turnController } from '../app/turnController.js'
import { getTurnState, resetTurnState } from '../app/turnStore.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'

// Streaming watchdog: if the backend goes silent while the session is still
// busy, the UI must auto-release instead of freezing forever. This is the
// fix for the "desktop hangs until you send another message" class of bugs
// (upstream drop, compression deadlock, gateway hiccup with no completion
// event delivered).
describe('turnController streaming watchdog', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    resetUiState()
    resetTurnState()
    turnController.fullReset()
    patchUiState({ busy: true, status: 'running…' })
  })

  afterEach(() => {
    vi.useRealTimers()
    turnController.fullReset()
    resetUiState()
    resetTurnState()
  })

  it('releases the busy lock when no stream event arrives within the stall window', () => {
    // Simulate the first streamed token arriving — arms the watchdog.
    turnController.recordMessageDelta({ text: 'hello ' })

    // Backend goes silent. Advance past the stall timeout with no further
    // event (this is the "frozen" scenario the watchdog exists for).
    vi.advanceTimersByTime(46000)

    expect(getUiState().busy).toBe(false)
    // The stall notice was surfaced so the user knows why it released.
    const activity = getTurnState().activity
    expect(activity.some((a: { text: string }) => a.text.includes('响应中断'))).toBe(true)
  })

  it('does NOT fire while stream events keep arriving (normal long turn)', () => {
    turnController.recordMessageDelta({ text: 'a' })
    // Keep receiving tokens just under the stall window repeatedly.
    for (let i = 0; i < 10; i++) {
      vi.advanceTimersByTime(20000)
      turnController.recordMessageDelta({ text: 'b' })
    }
    expect(getUiState().busy).toBe(true)
  })

  it('disarms and does not fire after a normal message.complete', () => {
    turnController.recordMessageDelta({ text: 'a' })
    // A real completion edge releases the lock (idle() disarms the watchdog).
    turnController.recordMessageComplete({ text: 'done', finalText: 'done' })
    expect(getUiState().busy).toBe(false)

    // Even if the stall window later elapses, nothing re-fires.
    vi.advanceTimersByTime(46000)
    expect(getUiState().busy).toBe(false)
  })

  it('fires on silence even for tool/progress activity that then stalls', () => {
    // Tool activity arms the watchdog via pushActivity.
    turnController.pushActivity('calling tool', 'info')
    vi.advanceTimersByTime(46000)
    expect(getUiState().busy).toBe(false)
  })
})
