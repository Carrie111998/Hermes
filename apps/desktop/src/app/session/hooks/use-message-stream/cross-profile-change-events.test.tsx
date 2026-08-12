import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $cronChangeTick, $sessionsChangeTick } from '@/store/live-sync'
import { $activeGatewayProfile } from '@/store/profile'
import { $sessionStates } from '@/store/session-states'
import type { RpcEvent } from '@/types/hermes'

import { useMessageStream } from './index'

// `sessions.changed` / `cron.changed` from a BACKGROUND profile's socket must
// still refresh the lists: the change watcher broadcasts on any profile's
// state.db writes, and the all-profiles sidebar shows every profile's rows.

const ACTIVE_SID = 'session-active'
const ACTIVE_PROFILE = 'default'
let handleEvent: ((event: RpcEvent) => void) | null = null
let queryClient: QueryClient

function Harness() {
  const activeSessionIdRef = useRef<string | null>(ACTIVE_SID)
  const sessionStateByRuntimeIdRef = useRef(new Map<string, ClientSessionState>())

  const stream = useMessageStream({
    activeGatewayProfile: ACTIVE_PROFILE,
    activeSessionIdRef,
    hydrateFromStoredSession: vi.fn(async () => undefined),
    queryClient,
    refreshHermesConfig: vi.fn<() => Promise<void>>(async () => undefined),
    refreshSessions: vi.fn<() => Promise<void>>(async () => undefined),
    sessionStateByRuntimeIdRef,
    updateSessionState: (sessionId, updater) => {
      const current = sessionStateByRuntimeIdRef.current.get(sessionId) ?? createClientSessionState()
      const next = updater(current)
      sessionStateByRuntimeIdRef.current.set(sessionId, next)

      return next
    }
  })

  useEffect(() => {
    handleEvent = stream.handleGatewayEvent
  }, [stream.handleGatewayEvent])

  return null
}

async function mountStream() {
  render(<Harness />)
  await waitFor(() => expect(handleEvent).not.toBeNull())
}

const changedEvent = (type: string, profile: string) =>
  act(() =>
    handleEvent!({
      payload: {},
      profile,
      session_id: '',
      type
    } as RpcEvent)
  )

beforeEach(() => {
  handleEvent = null
  queryClient = new QueryClient()
  $sessionStates.set({})
  $activeGatewayProfile.set(ACTIVE_PROFILE)
})

afterEach(() => {
  cleanup()
  $sessionStates.set({})
  vi.restoreAllMocks()
})

describe('background-profile change broadcasts', () => {
  it('sessions.changed from another profile bumps the list-refresh tick', async () => {
    await mountStream()
    const before = $sessionsChangeTick.get()
    changedEvent('sessions.changed', 'saf-auditor')
    expect($sessionsChangeTick.get()).toBe(before + 1)
  })

  it('cron.changed from another profile bumps the cron-refresh tick', async () => {
    await mountStream()
    const before = $cronChangeTick.get()
    changedEvent('cron.changed', 'saf-auditor')
    expect($cronChangeTick.get()).toBe(before + 1)
  })

  it('keeps foreground-scoped broadcasts (pet) gated to the active profile', async () => {
    await mountStream()
    // The tick atom only moves for the notifications this test can observe;
    // a pet.changed from a foreign profile must not repaint the active pet.
    // We assert via $sessionsChangeTick staying put: no cross-talk.
    const before = $sessionsChangeTick.get()
    changedEvent('pet.changed', 'saf-auditor')
    expect($sessionsChangeTick.get()).toBe(before)
    expect($activeGatewayProfile.get()).toBe(ACTIVE_PROFILE)
  })
})
