import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render } from '@testing-library/react'
import { type MutableRefObject, useEffect, useRef } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import {
  $activeSessionStoredIdRotation,
  setActiveSessionId,
  setActiveSessionStoredIdRotation,
  setSelectedStoredSessionId
} from '@/store/session'
import type { RpcEvent } from '@/types/hermes'

import { useSessionStateCache } from '../use-session-state-cache'

import { useMessageStream } from './index'

const RUNTIME_ID = 'runtime-collision'
const FOREGROUND_STORED_ID = 'default-stored'

interface HarnessHandle {
  dispatch: (event: RpcEvent) => void
  state: () => ClientSessionState | undefined
}

function Harness({ onReady }: { onReady: (handle: HarnessHandle) => void }) {
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
      state => ({ ...state, branch: 'foreground', profile: 'default' }),
      FOREGROUND_STORED_ID
    )
    onReady({
      dispatch: stream.handleGatewayEvent,
      state: () => sessionStateByRuntimeIdRef.current.get(RUNTIME_ID)
    })
  }, [onReady, sessionStateByRuntimeIdRef, stream.handleGatewayEvent, updateSessionState])

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
      nextStoredSessionId: 'default-stored-next',
      previousStoredSessionId: FOREGROUND_STORED_ID,
      profile: 'default',
      runtimeSessionId: RUNTIME_ID
    })
  })
})
