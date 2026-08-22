import { renderHook } from '@testing-library/react'
import { act } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'

import { MAIN_COMPOSER_SCOPE } from './composer/scope'

const requestGatewayMock = vi.hoisted(() => vi.fn())

vi.mock('@/app/gateway/hooks/use-gateway-request', () => ({
  useGatewayRequest: () => ({ requestGateway: requestGatewayMock })
}))

const { setSessionTileDelegate } = await import('@/store/session-states')
const { useSessionTileActions } = await import('./session-tile-actions')

const RUNTIME_SESSION_ID = 'rt-tile-current'
const STORED_SESSION_ID = 'stored-tile-db'
const RECOVERED_SESSION_ID = 'rt-tile-recovered'
let tileStates: Map<string, ClientSessionState>

function createTileState(): ClientSessionState {
  return {
    adoptedRunningTurn: false,
    awaitingResponse: false,
    branch: '',
    busy: false,
    cwd: '',
    fast: false,
    interimBoundaryPending: false,
    interrupted: false,
    lastActivityAt: null,
    lastActivityDescription: '',
    messages: [],
    model: '',
    needsInput: false,
    pendingBranchGroup: null,
    personality: '',
    provider: '',
    reasoningEffort: '',
    sawAssistantPayload: false,
    serviceTier: '',
    storedSessionId: STORED_SESSION_ID,
    streamId: null,
    turnLive: false,
    turnStartedAt: null,
    usage: null,
    yolo: false
  }
}

function tileState(runtimeId = RUNTIME_SESSION_ID): ClientSessionState {
  return tileStates.get(runtimeId) ?? createTileState()
}

function renderTileActions() {
  return renderHook(() =>
    useSessionTileActions({
      runtimeId: RUNTIME_SESSION_ID,
      scope: MAIN_COMPOSER_SCOPE,
      storedSessionId: STORED_SESSION_ID
    })
  )
}

// A tile's cancelRun/steerPrompt/reloadFromMessage each build their own
// requestGateway call directly instead of going through the shared
// submitPromptText pipeline (which already wraps its call in
// withSessionNotFoundResume) — see use-prompt-actions/index.test.tsx's
// "sleep/wake session recovery" suite for the same regression on the
// primary chat's own reloadFromMessage.
describe('useSessionTileActions sleep/wake session recovery', () => {
  beforeEach(() => {
    tileStates = new Map([[RUNTIME_SESSION_ID, createTileState()]])

    setSessionTileDelegate({
      archiveSession: vi.fn(async () => undefined),
      branchSession: vi.fn(async () => undefined),
      deleteSession: vi.fn(async () => undefined),
      executeSlash: vi.fn(async () => undefined),
      interruptSession: vi.fn(async () => undefined),
      resumeTile: vi.fn(async () => RUNTIME_SESSION_ID),
      submitToSession: vi.fn(async () => undefined),
      updateSession: vi.fn((runtimeId, updater) => {
        const next = updater(tileState(runtimeId))
        tileStates.set(runtimeId, next)

        return next
      })
    })
  })

  afterEach(() => {
    requestGatewayMock.mockReset()
    vi.restoreAllMocks()
  })

  it('resumes the stored session and retries once when session.interrupt reports "session not found"', async () => {
    const calls: { method: string; params?: Record<string, unknown> }[] = []
    let interruptAttempts = 0

    requestGatewayMock.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      calls.push({ method, params })

      if (method === 'session.interrupt') {
        interruptAttempts += 1

        if (interruptAttempts === 1) {
          throw new Error('session not found')
        }

        return {}
      }

      if (method === 'session.resume') {
        return { session_id: RECOVERED_SESSION_ID }
      }

      return {}
    })

    const { result } = renderTileActions()

    await act(async () => {
      await result.current.cancelRun()
    })

    // First interrupt (stale id) → session.resume (stored id) → retry interrupt (fresh id).
    expect(calls.map(c => c.method)).toEqual(['session.interrupt', 'session.resume', 'session.interrupt'])
    expect(calls[0]?.params).toEqual({ session_id: RUNTIME_SESSION_ID })
    expect(calls[1]?.params).toEqual({ session_id: STORED_SESSION_ID, source: 'desktop', omit_messages: true })
    expect(calls[2]?.params).toEqual({ session_id: RECOVERED_SESSION_ID })
  })

  it('resumes the stored session and retries once when session.redirect (steer) reports "session not found"', async () => {
    const calls: { method: string; params?: Record<string, unknown> }[] = []
    let redirectAttempts = 0

    requestGatewayMock.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      calls.push({ method, params })

      if (method === 'session.redirect') {
        redirectAttempts += 1

        if (redirectAttempts === 1) {
          throw new Error('session not found')
        }

        return { status: 'queued' }
      }

      if (method === 'session.resume') {
        return { session_id: RECOVERED_SESSION_ID }
      }

      return {}
    })

    const { result } = renderTileActions()

    const ok = await act(async () => result.current.steerPrompt('actually use Postgres'))

    expect(ok).toBe(true)
    expect(calls.map(c => c.method)).toEqual(['session.redirect', 'session.resume', 'session.redirect'])
    expect(calls[0]?.params).toMatchObject({ session_id: RUNTIME_SESSION_ID, text: 'actually use Postgres' })
    expect(calls[2]?.params).toMatchObject({ session_id: RECOVERED_SESSION_ID, text: 'actually use Postgres' })
    expect(calls[0]?.params?.client_message_id).toBe(calls[2]?.params?.client_message_id)
    expect(calls[2]?.params?.client_message_id).toMatch(/^user-/)
    expect(tileState(RUNTIME_SESSION_ID).messages).toHaveLength(0)
    expect(tileState(RECOVERED_SESSION_ID).messages).toHaveLength(1)
    expect(tileState(RECOVERED_SESSION_ID).messages[0]).toMatchObject({
      id: calls[2]?.params?.client_message_id,
      deliveryState: 'queued'
    })
  })

  it('marks the same Arabic question bubble queued when session.redirect queues it', async () => {
    const calls: { method: string; params?: Record<string, unknown> }[] = []

    requestGatewayMock.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      calls.push({ method, params })

      if (method === 'session.redirect') {
        return { status: 'queued' }
      }

      return {}
    })

    const { result } = renderTileActions()

    const ok = await act(async () => result.current.steerPrompt('؟'))

    expect(ok).toBe(true)
    expect(calls).toHaveLength(1)
    expect(calls[0]?.params).toMatchObject({
      session_id: RUNTIME_SESSION_ID,
      text: '؟'
    })
    expect(calls[0]?.params?.client_message_id).toMatch(/^user-/)
    expect(tileState().messages).toHaveLength(1)
    expect(tileState().messages[0]).toMatchObject({
      id: calls[0]?.params?.client_message_id,
      role: 'user',
      deliveryState: 'queued'
    })
    expect(tileState().messages[0]?.parts).toEqual([{ type: 'text', text: '؟' }])
  })

  it('keeps a tool-boundary redirect queued until model progress proves it was applied', async () => {
    requestGatewayMock.mockImplementation(async method =>
      method === 'session.redirect' ? { status: 'queued', delivery: 'tool_boundary' } : {}
    )

    const { result } = renderTileActions()

    expect(await act(async () => result.current.steerPrompt('غيّر الخطة'))).toBe(true)
    expect(tileState().messages).toHaveLength(1)
    expect(tileState().messages[0]).toMatchObject({
      role: 'user',
      deliveryState: 'queued',
      deliveryClearsOnProgress: true
    })
  })
})
