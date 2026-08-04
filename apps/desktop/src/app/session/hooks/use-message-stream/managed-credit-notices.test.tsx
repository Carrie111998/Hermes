import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'
import { dispatchNativeNotification } from '@/store/native-notifications'
import { $notifications, clearNotifications } from '@/store/notifications'
import type { RpcEvent } from '@/types/hermes'

import { useMessageStream } from './index'

vi.mock('@/store/native-notifications', () => ({
  dispatchNativeNotification: vi.fn()
}))

const SID = 'session-1'

let handleEvent: ((event: RpcEvent) => void) | null = null
let queryClient: QueryClient

function Harness() {
  const activeSessionIdRef = useRef<string | null>(SID)
  const sessionStateByRuntimeIdRef = useRef(new Map<string, ClientSessionState>())

  const stream = useMessageStream({
    activeSessionIdRef,
    hydrateFromStoredSession: vi.fn(async () => undefined),
    queryClient,
    refreshHermesConfig: vi.fn(async () => undefined),
    refreshSessions: vi.fn(async () => undefined),
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

function setManaged(managed: boolean) {
  if (!managed) {
    Reflect.deleteProperty(window, 'hermesDesktop')

    return
  }

  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: { eva: {} },
    writable: true
  })
}

async function mountStream(managed: boolean) {
  setManaged(managed)
  render(<Harness />)
  await waitFor(() => expect(handleEvent).not.toBeNull())
}

function showNotice(payload: Record<string, unknown>) {
  act(() => handleEvent!({ payload, session_id: SID, type: 'notification.show' }))
}

describe('managed credits notification boundary', () => {
  beforeEach(() => {
    handleEvent = null
    queryClient = new QueryClient()
    vi.spyOn(queryClient, 'invalidateQueries').mockResolvedValue(undefined)
    clearNotifications()
    vi.mocked(dispatchNativeNotification).mockClear()
  })

  afterEach(() => {
    cleanup()
    clearNotifications()
    Reflect.deleteProperty(window, 'hermesDesktop')
    vi.restoreAllMocks()
  })

  it.each([
    ['credits.usage', "• You've used $110.00 of your $220.00 cap"],
    ['credits.grant_spent', '• Grant spent · $12.00 top-up left'],
    ['credits.depleted', '✕ Credit access paused · run /topup to top up']
  ])('suppresses managed %s before toast, native, and billing invalidation', async (key, text) => {
    await mountStream(true)

    showNotice({ key, kind: 'sticky', level: 'error', text })

    expect($notifications.get()).toEqual([])
    expect(dispatchNativeNotification).not.toHaveBeenCalled()
    expect(queryClient.invalidateQueries).not.toHaveBeenCalled()
  })

  it('preserves managed non-credit notices', async () => {
    await mountStream(true)

    showNotice({ key: 'agent.starting', kind: 'ttl', level: 'info', text: 'Agent is still starting.' })

    expect($notifications.get()).toHaveLength(1)
    expect($notifications.get()[0]?.message).toBe('Agent is still starting.')
  })

  it('preserves unmanaged credit notices and their billing refresh', async () => {
    await mountStream(false)

    showNotice({
      key: 'credits.depleted',
      kind: 'sticky',
      level: 'error',
      text: '✕ Credit access paused · run /topup to top up'
    })

    expect($notifications.get()[0]).toMatchObject({
      id: 'credits.depleted',
      message: 'Credit access paused',
      meta: 'run /topup to top up'
    })
    expect(dispatchNativeNotification).toHaveBeenCalledWith(
      expect.objectContaining({
        body: '✕ Credit access paused · run /topup to top up',
        kind: 'credits'
      })
    )
    expect(queryClient.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['billing', 'state'] })
  })
})
