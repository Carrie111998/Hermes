import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render } from '@testing-library/react'
import { type MutableRefObject, useEffect, useRef } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import {
  $activeSessionId,
  $activeSessionStoredIdRotation,
  setActiveSessionId,
  setActiveSessionStoredIdRotation,
  setSelectedStoredSessionId
} from '@/store/session'
import type { RpcEvent } from '@/types/hermes'

import { useSessionStateCache } from '../use-session-state-cache'

import { useMessageStream } from './index'

const RUNTIME_ID = 'runtime-collision'
const REBUILT_RUNTIME_ID = 'runtime-rebuilt'
const FOREGROUND_STORED_ID = 'default-stored'

interface HarnessHandle {
  dispatch: (event: RpcEvent) => void
  state: (runtimeId?: string) => ClientSessionState | undefined
}

function Harness({ connectionId, onReady }: { connectionId?: string; onReady: (handle: HarnessHandle) => void }) {
  const busyRef: MutableRefObject<boolean> = { current: false }
  const queryClientRef = useRef(new QueryClient())

  const cache = useSessionStateCache({
    activeSessionId: RUNTIME_ID,
    busyRef,
    selectedStoredSessionId: FOREGROUND_STORED_ID,
    setAwaitingResponse: () => undefined,
    setBusy: () => undefined,
    setMessages: () => undefined
  })

  const { sessionStateByRuntimeIdRef, updateSessionState } = cache

  const stream = useMessageStream({
    activeGatewayProfile: 'default',
    activeSessionIdRef: cache.activeSessionIdRef,
    hydrateFromStoredSession: vi.fn(async () => undefined),
    queryClient: queryClientRef.current,
    refreshHermesConfig: vi.fn(async () => undefined),
    refreshSessions: vi.fn(async () => undefined),
    sessionStateByRuntimeIdRef,
    updateSessionState
  })

  useEffect(() => {
    updateSessionState(
      RUNTIME_ID,
      state => ({ ...state, branch: 'foreground', connectionId: connectionId ?? null, profile: 'default' }),
      FOREGROUND_STORED_ID
    )
    onReady({
      dispatch: stream.handleGatewayEvent,
      state: (runtimeId = RUNTIME_ID) => sessionStateByRuntimeIdRef.current.get(runtimeId)
    })
  }, [connectionId, onReady, sessionStateByRuntimeIdRef, stream.handleGatewayEvent, updateSessionState])

  return null
}

describe('session.info profile ownership', () => {
  afterEach(() => {
    cleanup()
    setActiveSessionId(null)
    setActiveSessionStoredIdRotation(null)
    setSelectedStoredSessionId(null)
    vi.restoreAllMocks()
  })

  it('rejects a colliding background profile before it can rotate the foreground runtime', () => {
    let handle: HarnessHandle | null = null

    setActiveSessionId(RUNTIME_ID)
    setSelectedStoredSessionId(FOREGROUND_STORED_ID)
    render(<Harness onReady={value => (handle = value)} />)

    const foreground = handle!.state()

    act(() => {
      handle!.dispatch({
        payload: {
          branch: 'background',
          running: true,
          stored_session_id: 'meta-stored-next'
        },
        profile: 'meta',
        session_id: RUNTIME_ID,
        type: 'session.info'
      })
    })

    expect(handle!.state()).toBe(foreground)
    expect(handle!.state()).toMatchObject({
      branch: 'foreground',
      busy: false,
      profile: 'default',
      storedSessionId: FOREGROUND_STORED_ID
    })
    expect($activeSessionStoredIdRotation.get()).toBeNull()
  })

  it('keeps same-profile compression rotation profile-qualified', () => {
    let handle: HarnessHandle | null = null

    setActiveSessionId(RUNTIME_ID)
    setSelectedStoredSessionId(FOREGROUND_STORED_ID)
    render(<Harness onReady={value => (handle = value)} />)

    act(() => {
      handle!.dispatch({
        payload: { branch: 'continued', stored_session_id: 'default-stored-next' },
        profile: 'default',
        session_id: RUNTIME_ID,
        type: 'session.info'
      })
    })

    expect(handle!.state()).toMatchObject({
      branch: 'continued',
      profile: 'default',
      storedSessionId: 'default-stored-next'
    })
    expect($activeSessionStoredIdRotation.get()).toEqual({
      connectionId: null,
      nextStoredSessionId: 'default-stored-next',
      previousStoredSessionId: FOREGROUND_STORED_ID,
      profile: 'default',
      runtimeSessionId: RUNTIME_ID
    })
  })

  it('rejects a colliding runtime from another connection with the same profile', () => {
    let handle: HarnessHandle | null = null

    setActiveSessionId(RUNTIME_ID)
    setSelectedStoredSessionId(FOREGROUND_STORED_ID)
    render(<Harness connectionId="gateway-a" onReady={value => (handle = value)} />)

    const foreground = handle!.state()

    act(() => {
      handle!.dispatch({
        connectionId: 'gateway-b',
        payload: { branch: 'foreign', running: true, stored_session_id: FOREGROUND_STORED_ID },
        profile: 'default',
        session_id: RUNTIME_ID,
        type: 'session.info'
      })
    })

    expect(handle!.state()).toBe(foreground)
    expect(handle!.state()).toMatchObject({ branch: 'foreground', busy: false, connectionId: 'gateway-a' })
  })

  it('does not publish a stored-id rotation from another connection with the same profile', () => {
    let handle: HarnessHandle | null = null

    setActiveSessionId(RUNTIME_ID)
    setSelectedStoredSessionId(FOREGROUND_STORED_ID)
    render(<Harness connectionId="gateway-a" onReady={value => (handle = value)} />)

    act(() => {
      handle!.dispatch({
        connectionId: 'gateway-b',
        payload: { branch: 'foreign', stored_session_id: 'foreign-next' },
        profile: 'default',
        session_id: RUNTIME_ID,
        type: 'session.info'
      })
    })

    expect(handle!.state()?.storedSessionId).toBe(FOREGROUND_STORED_ID)
    expect($activeSessionStoredIdRotation.get()).toBeNull()
  })

  it('does not adopt a rebuilt runtime announced by another connection with the same profile', () => {
    let handle: HarnessHandle | null = null

    setActiveSessionId(RUNTIME_ID)
    setSelectedStoredSessionId(FOREGROUND_STORED_ID)
    render(<Harness connectionId="gateway-a" onReady={value => (handle = value)} />)

    act(() => {
      handle!.dispatch({
        connectionId: 'gateway-b',
        payload: { stored_session_id: FOREGROUND_STORED_ID },
        profile: 'default',
        session_id: REBUILT_RUNTIME_ID,
        type: 'session.info'
      })
    })

    expect($activeSessionId.get()).toBe(RUNTIME_ID)
    expect(handle!.state(REBUILT_RUNTIME_ID)).toBeUndefined()
  })

  it('adopts a rebuilt runtime announced by the same connection and profile', () => {
    let handle: HarnessHandle | null = null

    setActiveSessionId(RUNTIME_ID)
    setSelectedStoredSessionId(FOREGROUND_STORED_ID)
    render(<Harness connectionId="gateway-a" onReady={value => (handle = value)} />)

    act(() => {
      handle!.dispatch({
        connectionId: 'gateway-a',
        payload: { branch: 'rebuilt', stored_session_id: FOREGROUND_STORED_ID },
        profile: 'default',
        session_id: REBUILT_RUNTIME_ID,
        type: 'session.info'
      })
    })

    expect($activeSessionId.get()).toBe(REBUILT_RUNTIME_ID)
    expect(handle!.state(REBUILT_RUNTIME_ID)).toMatchObject({
      branch: 'rebuilt',
      connectionId: 'gateway-a',
      profile: 'default',
      storedSessionId: FOREGROUND_STORED_ID
    })
  })
})
