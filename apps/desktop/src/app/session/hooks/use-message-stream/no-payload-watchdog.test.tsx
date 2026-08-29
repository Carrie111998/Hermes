// A turn that went live (message.start) but whose backend died before producing
// ANY assistant payload leaves turnLive=busy=true forever — no message.complete
// arrives, and a dead gateway stops heartbeating, so the existing session.info
// running=false settle never fires. The 5-min session watchdog is the only
// existing backstop; the no-payload watchdog bounds that wait to 60s.
import { act, cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'

import { type MessageStreamHarness, renderMessageStream } from './test-harness'
import { NO_PAYLOAD_WATCHDOG_MS } from './index'

const SID = 'no-payload-watchdog-session'

let stream: MessageStreamHarness

async function mountHarness() {
  vi.useFakeTimers()
  stream = renderMessageStream(SID)
  await act(async () => {
    await Promise.resolve()
  })
}

describe('no-payload watchdog settles a live turn that produced nothing', () => {
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('force-settles a live turn with no payload after the watchdog fires', async () => {
    await mountHarness()

    // Seed state: turn went live, backend died before any assistant payload.
    const seeded = createClientSessionState()
    stream.states.set(SID, {
      ...seeded,
      turnLive: true,
      busy: true,
      awaitingResponse: true,
      sawAssistantPayload: false,
      turnStartedAt: Date.now() - NO_PAYLOAD_WATCHDOG_MS - 1
    })

    // Advance past the 5s watchdog interval.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000)
    })

    const state = stream.state(SID)
    expect(state.busy).toBe(false)
    expect(state.awaitingResponse).toBe(false)
    expect(state.turnLive).toBe(false)
    expect(state.turnStartedAt).toBeNull()
    expect(state.streamId).toBeNull()
  })

  it('does NOT settle a live turn that has produced payload', async () => {
    await mountHarness()

    const seeded = createClientSessionState()
    stream.states.set(SID, {
      ...seeded,
      turnLive: true,
      busy: true,
      awaitingResponse: true,
      sawAssistantPayload: true, // payload arrived — watchdog must not fire
      turnStartedAt: Date.now() - NO_PAYLOAD_WATCHDOG_MS - 1
    })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000)
    })

    const state = stream.state(SID)
    expect(state.busy).toBe(true)
    expect(state.turnLive).toBe(true)
  })

  it('does NOT settle a live turn before the watchdog deadline', async () => {
    await mountHarness()

    const seeded = createClientSessionState()
    stream.states.set(SID, {
      ...seeded,
      turnLive: true,
      busy: true,
      awaitingResponse: true,
      sawAssistantPayload: false,
      turnStartedAt: Date.now() - 10_000 // only 10s ago, watchdog is 60s
    })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000)
    })

    const state = stream.state(SID)
    expect(state.busy).toBe(true)
    expect(state.turnLive).toBe(true)
  })
})
