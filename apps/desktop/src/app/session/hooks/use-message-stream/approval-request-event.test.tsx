import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'
import * as gatewayStore from '@/store/gateway'
import { dispatchNativeNotification } from '@/store/native-notifications'
import { $activeGatewayProfile } from '@/store/profile'
import { clearAllPrompts, sessionApprovalRequest } from '@/store/prompts'
import { sessionProviderWait, setSessionProviderWait } from '@/store/provider-wait'
import { $activeSessionId } from '@/store/session'
import { clearAllSessionStates, recordSessionEventScope } from '@/store/session-states'
import { sessionDraftingTool, setSessionDraftingTool } from '@/store/tool-drafting'
import type { RpcEvent } from '@/types/hermes'

import { useMessageStream } from './index'

vi.mock('@/store/native-notifications', () => ({ dispatchNativeNotification: vi.fn() }))

const ACTIVE_SID = 'session-active'

const sourceAApproval = () =>
  sessionApprovalRequest(ACTIVE_SID, { connectionId: 'remote-a', profile: 'research' }).get()

let handleEvent: ((event: RpcEvent) => void) | null = null
let stateBySession: Map<string, ClientSessionState> | null = null

function Harness() {
  const activeSessionIdRef = useRef<string | null>(ACTIVE_SID)
  const sessionStateByRuntimeIdRef = useRef(new Map<string, ClientSessionState>())
  stateBySession = sessionStateByRuntimeIdRef.current
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

describe('approval.request event', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    handleEvent = null
    stateBySession = null
    clearAllSessionStates()
    clearAllPrompts()
    $activeGatewayProfile.set('default')
    $activeSessionId.set(ACTIVE_SID)
    gatewayStore.$gateway.set(null)
    render(<Harness />)
    await waitFor(() => expect(handleEvent).not.toBeNull())
  })

  afterEach(() => {
    cleanup()
    clearAllSessionStates()
    clearAllPrompts()
    $activeSessionId.set(null)
    gatewayStore.$gateway.set(null)
    vi.restoreAllMocks()
  })

  it('binds the parked prompt, receipt ack and notification to the emitting source', async () => {
    const activeRequest = vi.fn()
    const routedRequest = vi.spyOn(gatewayStore, 'requestGatewayForAgent').mockResolvedValue({} as never)
    gatewayStore.$gateway.set({ request: activeRequest } as never)

    act(() =>
      handleEvent!({
        payload: {
          choices: ['once', 'deny'],
          description: 'redacted',
          request_id: 'test-request-id'
        },
        connectionId: 'remote-a',
        profile: 'research',
        session_id: ACTIVE_SID,
        type: 'approval.request'
      })
    )

    expect(sourceAApproval()).toMatchObject({
      connectionId: 'remote-a',
      description: 'redacted',
      profile: 'research',
      requestId: 'test-request-id',
      sessionId: ACTIVE_SID
    })
    expect(stateBySession?.get(ACTIVE_SID)?.needsInput).not.toBe(true)
    expect(dispatchNativeNotification).toHaveBeenCalledWith(
      expect.objectContaining({
        approvalConnectionId: 'remote-a',
        approvalProfile: 'research',
        approvalRequestId: 'test-request-id',
        sessionId: ACTIVE_SID
      })
    )
    await waitFor(() =>
      expect(routedRequest).toHaveBeenCalledWith('remote-a', 'research', 'approval.received', {
        request_id: 'test-request-id',
        session_id: ACTIVE_SID
      })
    )
    expect(activeRequest).not.toHaveBeenCalled()
  })

  it('marks needsInput only for an approval from the active source', () => {
    act(() =>
      handleEvent!({
        payload: { description: 'active approval', request_id: 'active-request' },
        profile: 'default',
        session_id: ACTIVE_SID,
        type: 'approval.request'
      })
    )

    expect(stateBySession?.get(ACTIVE_SID)?.needsInput).toBe(true)
  })

  it('keeps legacy ID-free approvals inline and omits unsafe native response actions', async () => {
    const routedRequest = vi.spyOn(gatewayStore, 'requestGatewayForAgent').mockResolvedValue({} as never)

    act(() =>
      handleEvent!({
        connectionId: 'remote-a',
        payload: { description: 'legacy approval' },
        profile: 'research',
        session_id: ACTIVE_SID,
        type: 'approval.request'
      })
    )

    expect(sourceAApproval()).toMatchObject({
      connectionId: 'remote-a',
      description: 'legacy approval',
      profile: 'research',
      requestId: undefined,
      sessionId: ACTIVE_SID
    })
    expect(dispatchNativeNotification).toHaveBeenCalledWith(
      expect.objectContaining({
        actions: undefined,
        approvalConnectionId: 'remote-a',
        approvalProfile: 'research',
        approvalRequestId: undefined,
        sessionId: ACTIVE_SID
      })
    )
    expect(routedRequest).not.toHaveBeenCalled()
  })

  it('keeps a foreign connection from claiming the active runtime when the profile matches', () => {
    const before = { ...createClientSessionState(), busy: true, needsInput: true }
    stateBySession?.set(ACTIVE_SID, before)

    act(() =>
      handleEvent!({
        connectionId: 'remote-b',
        payload: { text: 'foreign completion' },
        profile: 'default',
        session_id: ACTIVE_SID,
        type: 'message.complete'
      })
    )

    expect(stateBySession?.get(ACTIVE_SID)).toEqual(before)
  })

  it('keeps a foreign profile from claiming the active runtime when the connection matches', () => {
    const before = { ...createClientSessionState(), busy: true, needsInput: true }
    stateBySession?.set(ACTIVE_SID, before)

    act(() =>
      handleEvent!({
        payload: { text: 'foreign completion' },
        profile: 'research',
        session_id: ACTIVE_SID,
        type: 'message.complete'
      })
    )

    expect(stateBySession?.get(ACTIVE_SID)).toEqual(before)
  })

  it('keeps a foreign message.start from composing into the active runtime', () => {
    const before = { ...createClientSessionState(), needsInput: true }
    stateBySession?.set(ACTIVE_SID, before)

    act(() =>
      handleEvent!({
        connectionId: 'remote-b',
        payload: { message_id: 'foreign-message' },
        profile: 'default',
        session_id: ACTIVE_SID,
        type: 'message.start'
      })
    )

    expect(stateBySession?.get(ACTIVE_SID)).toEqual(before)
  })

  it('does not clear an approval when another source completes the same session id', async () => {
    vi.spyOn(gatewayStore, 'requestGatewayForAgent').mockResolvedValue({} as never)

    act(() =>
      handleEvent!({
        connectionId: 'remote-a',
        payload: { description: 'source A', request_id: 'source-a-request' },
        profile: 'research',
        session_id: ACTIVE_SID,
        type: 'approval.request'
      })
    )

    await waitFor(() => expect(sourceAApproval()?.requestId).toBe('source-a-request'))

    act(() =>
      handleEvent!({
        connectionId: 'remote-b',
        payload: { text: 'source B completed' },
        profile: 'research',
        session_id: ACTIVE_SID,
        type: 'message.complete'
      })
    )

    expect(sourceAApproval()?.requestId).toBe('source-a-request')

    act(() =>
      handleEvent!({
        connectionId: 'remote-a',
        payload: { text: 'source A completed' },
        profile: 'research',
        session_id: ACTIVE_SID,
        type: 'message.complete'
      })
    )

    expect(sourceAApproval()).toBeNull()
  })

  it('does not settle active runtime state when another source completes the same session id', () => {
    // Model the production registry fan-in that used to let the first foreign
    // event claim the shared runtime id before the active handler saw it.
    recordSessionEventScope({ connectionId: 'remote-b', profile: 'research', session_id: ACTIVE_SID })

    act(() =>
      handleEvent!({
        payload: { description: 'active approval', request_id: 'active-request' },
        profile: 'default',
        session_id: ACTIVE_SID,
        type: 'approval.request'
      })
    )

    const before = stateBySession?.get(ACTIVE_SID)

    expect(before?.needsInput).toBe(true)
    setSessionProviderWait(ACTIVE_SID, 'waiting on provider')
    setSessionDraftingTool(ACTIVE_SID, 'write_file')

    act(() =>
      handleEvent!({
        connectionId: 'remote-b',
        payload: { text: 'foreign completion' },
        profile: 'research',
        session_id: ACTIVE_SID,
        type: 'message.complete'
      })
    )

    expect(stateBySession?.get(ACTIVE_SID)).toEqual(before)
    expect(sessionProviderWait(ACTIVE_SID).get()).toBe('waiting on provider')
    expect(sessionDraftingTool(ACTIVE_SID).get()?.name).toBe('write_file')
  })

  it('does not clear an approval when another source errors for the same session id', async () => {
    vi.spyOn(gatewayStore, 'requestGatewayForAgent').mockResolvedValue({} as never)

    act(() =>
      handleEvent!({
        connectionId: 'remote-a',
        payload: { description: 'source A', request_id: 'source-a-request' },
        profile: 'research',
        session_id: ACTIVE_SID,
        type: 'approval.request'
      })
    )

    await waitFor(() => expect(sourceAApproval()?.requestId).toBe('source-a-request'))

    act(() =>
      handleEvent!({
        connectionId: 'remote-b',
        payload: { message: 'source B errored' },
        profile: 'research',
        session_id: ACTIVE_SID,
        type: 'error'
      })
    )

    expect(sourceAApproval()?.requestId).toBe('source-a-request')

    act(() =>
      handleEvent!({
        connectionId: 'remote-a',
        payload: { message: 'source A errored' },
        profile: 'research',
        session_id: ACTIVE_SID,
        type: 'error'
      })
    )

    expect(sourceAApproval()).toBeNull()
  })

  it('does not fail active runtime state when another source errors for the same session id', () => {
    act(() =>
      handleEvent!({
        payload: { description: 'active approval', request_id: 'active-request' },
        profile: 'default',
        session_id: ACTIVE_SID,
        type: 'approval.request'
      })
    )

    const before = stateBySession?.get(ACTIVE_SID)

    expect(before?.needsInput).toBe(true)
    setSessionProviderWait(ACTIVE_SID, 'waiting on provider')
    setSessionDraftingTool(ACTIVE_SID, 'write_file')

    act(() =>
      handleEvent!({
        connectionId: 'remote-b',
        payload: { message: 'foreign error' },
        profile: 'research',
        session_id: ACTIVE_SID,
        type: 'error'
      })
    )

    expect(stateBySession?.get(ACTIVE_SID)).toEqual(before)
    expect(sessionProviderWait(ACTIVE_SID).get()).toBe('waiting on provider')
    expect(sessionDraftingTool(ACTIVE_SID).get()?.name).toBe('write_file')
  })

  it('does not apply session.info from another source sharing the active runtime id', () => {
    act(() =>
      handleEvent!({
        payload: { description: 'active approval', request_id: 'active-request' },
        profile: 'default',
        session_id: ACTIVE_SID,
        type: 'approval.request'
      })
    )

    const before = stateBySession?.get(ACTIVE_SID)

    expect(before?.needsInput).toBe(true)

    act(() =>
      handleEvent!({
        connectionId: 'remote-b',
        payload: { cwd: '/foreign', model: 'foreign-model', running: true },
        profile: 'research',
        session_id: ACTIVE_SID,
        type: 'session.info'
      })
    )

    expect(stateBySession?.get(ACTIVE_SID)).toEqual(before)
  })

  it('replays a pending approval through the gateway.ready or session.info source', async () => {
    const activeRequest = vi.fn()

    const routedRequest = vi.spyOn(gatewayStore, 'requestGatewayForAgent').mockImplementation(
      async (_connectionId, _profile, method) => {
        if (method === 'approval.pending') {
          return {
            approvals: [{ description: 'replayed', request_id: 'replayed-request-id' }]
          } as never
        }

        return { acknowledged: true } as never
      }
    )

    gatewayStore.$gateway.set({ request: activeRequest } as never)

    act(() =>
      handleEvent!({
        connectionId: 'remote-a',
        payload: { running: true },
        profile: 'research',
        session_id: ACTIVE_SID,
        type: 'session.info'
      })
    )

    await waitFor(() =>
      expect(routedRequest).toHaveBeenNthCalledWith(1, 'remote-a', 'research', 'approval.pending', {
        session_id: ACTIVE_SID
      })
    )
    await waitFor(() =>
      expect(sourceAApproval()).toMatchObject({
        connectionId: 'remote-a',
        profile: 'research',
        requestId: 'replayed-request-id',
        sessionId: ACTIVE_SID
      })
    )
    expect(routedRequest).toHaveBeenNthCalledWith(2, 'remote-a', 'research', 'approval.received', {
      request_id: 'replayed-request-id',
      session_id: ACTIVE_SID
    })
    expect(activeRequest).not.toHaveBeenCalled()
  })
})
