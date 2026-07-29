import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $activeGatewayProfile } from '@/store/profile'
import { $pendingSkinApplies, __resetBackendSkinSync } from '@/themes/backend-sync'
import type { RpcEvent } from '@/types/hermes'

import { useMessageStream } from './index'

let handleEvent: ((event: RpcEvent) => void) | null = null

function Harness() {
  const activeSessionIdRef = useRef<string | null>(null)
  const sessionStateByRuntimeIdRef = useRef(new Map<string, ClientSessionState>())
  const queryClientRef = useRef(new QueryClient())

  const stream = useMessageStream({
    activeSessionIdRef,
    hydrateFromStoredSession: vi.fn(async () => undefined),
    queryClient: queryClientRef.current,
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

describe('gateway.ready skin adoption', () => {
  beforeEach(() => {
    handleEvent = null
    window.localStorage.clear()
    __resetBackendSkinSync()
    $activeGatewayProfile.set('default')
  })

  afterEach(() => {
    cleanup()
    $activeGatewayProfile.set('default')
  })

  it('queues first-use adoption when a prewarmed profile connects in the background', async () => {
    render(<Harness />)
    await waitFor(() => expect(handleEvent).not.toBeNull())

    act(() =>
      handleEvent!({
        payload: {
          skin: {
            colors: { background: '#101020', ui_accent: '#ff33aa', ui_text: '#eeeeee' },
            name: 'neon'
          }
        },
        profile: 'work',
        type: 'gateway.ready'
      })
    )

    expect($pendingSkinApplies.get()).toEqual([{ name: 'neon', profile: 'work' }])
  })

  it('queues a runtime skin change for its background source profile', async () => {
    render(<Harness />)
    await waitFor(() => expect(handleEvent).not.toBeNull())

    act(() =>
      handleEvent!({
        payload: {
          colors: { background: '#202010', ui_accent: '#33ffaa', ui_text: '#eeeeee' },
          name: 'forest'
        },
        profile: 'work',
        type: 'skin.changed'
      })
    )

    expect($pendingSkinApplies.get()).toEqual([{ name: 'forest', profile: 'work' }])
  })
})
