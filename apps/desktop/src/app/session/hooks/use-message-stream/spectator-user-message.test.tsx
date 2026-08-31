import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { chatMessageText } from '@/lib/chat-messages'
import { createClientSessionState } from '@/lib/chat-runtime'
import type { RpcEvent } from '@/types/hermes'

import { useMessageStream } from './index'

const SID = 'runtime-1'
const sessionStates = new Map<string, ClientSessionState>()
let handleEvent: ((event: RpcEvent) => void) | null = null

function Harness() {
  const activeSessionIdRef = useRef<string | null>(SID)
  const sessionStateByRuntimeIdRef = useRef(sessionStates)
  const queryClientRef = useRef(new QueryClient())

  const stream = useMessageStream({
    activeSessionIdRef,
    hydrateFromStoredSession: vi.fn(async () => undefined),
    queryClient: queryClientRef.current,
    refreshHermesConfig: vi.fn(async () => undefined),
    refreshSessions: vi.fn(async () => undefined),
    sessionStateByRuntimeIdRef,
    updateSessionState: (sessionId, updater) => {
      const next = updater(sessionStates.get(sessionId) ?? createClientSessionState())
      sessionStates.set(sessionId, next)

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

function emit(payload: Record<string, unknown>) {
  act(() => handleEvent!({ payload, session_id: SID, type: 'message.user' }))
}

describe('spectator accepted user-message mirror', () => {
  const desktop = window.hermesDesktop
  const spectator = window.__HERMES_SPECTATOR__

  beforeEach(() => {
    handleEvent = null
    sessionStates.clear()
  })

  afterEach(() => {
    cleanup()
    window.hermesDesktop = desktop
    window.__HERMES_SPECTATOR__ = spectator
    vi.restoreAllMocks()
  })

  it('appends once on the browser spectator and ignores replay duplicates', async () => {
    delete (window as { hermesDesktop?: typeof window.hermesDesktop }).hermesDesktop
    window.__HERMES_SPECTATOR__ = true
    await mountStream()

    const payload = {
      display_kind: null,
      observer_id: 'observer-turn-1',
      text: 'Reply exactly: IPAD LIVE RELAY OK',
      timestamp: 123
    }

    emit(payload)
    emit(payload)

    const messages = sessionStates.get(SID)?.messages ?? []
    expect(messages).toHaveLength(1)
    expect(messages[0]).toMatchObject({ id: 'observer-turn-1', role: 'user', timestamp: 123 })
    expect(chatMessageText(messages[0]!)).toBe('Reply exactly: IPAD LIVE RELAY OK')
  })

  it('does not duplicate the writer surface optimistic bubble', async () => {
    window.hermesDesktop = desktop ?? ({} as typeof window.hermesDesktop)
    window.__HERMES_SPECTATOR__ = false
    await mountStream()

    emit({ observer_id: 'observer-turn-2', text: 'writer already painted me', timestamp: 123 })
    expect(sessionStates.get(SID)?.messages ?? []).toEqual([])
  })

  it('does not render hidden widget-intent messages', async () => {
    delete (window as { hermesDesktop?: typeof window.hermesDesktop }).hermesDesktop
    window.__HERMES_SPECTATOR__ = true
    await mountStream()

    emit({ display_kind: 'hidden', observer_id: 'hidden-turn', text: 'internal instruction', timestamp: 123 })
    expect(sessionStates.get(SID)?.messages ?? []).toEqual([])
  })
})
