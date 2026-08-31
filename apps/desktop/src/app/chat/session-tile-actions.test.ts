import { renderHook } from '@testing-library/react'
import { act } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createComposerAttachmentScope } from '@/store/composer'
import { requestGatewayForAgent } from '@/store/gateway'
import type { SessionOwnerRoute } from '@/store/session-request-router'

import { type ComposerScope, MAIN_COMPOSER_SCOPE } from './composer/scope'

const requestGatewayMock = vi.hoisted(() => vi.fn())

vi.mock('@/store/gateway', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  requestGatewayForAgent: vi.fn()
}))

const { $activeSessionId, $sessions, setSessions } = await import('@/store/session')
const { $sessionTiles, setSessionTileDelegate } = await import('@/store/session-states')
const { listTileSessionRow, useSessionTileActions } = await import('./session-tile-actions')

const RUNTIME_SESSION_ID = 'rt-tile-current'
const STORED_SESSION_ID = 'stored-tile-db'
const RECOVERED_SESSION_ID = 'rt-tile-recovered'

function renderTileActions({
  ownerRoute,
  runtimeId = RUNTIME_SESSION_ID,
  scope = MAIN_COMPOSER_SCOPE,
  storedSessionId = STORED_SESSION_ID
}: {
  ownerRoute?: SessionOwnerRoute
  runtimeId?: string
  scope?: ComposerScope
  storedSessionId?: string
} = {}) {
  return renderHook(() =>
    useSessionTileActions({
      ownerRoute,
      requestGateway: requestGatewayMock,
      runtimeId,
      scope,
      storedSessionId
    } as never)
  )
}

describe('session tile optimistic owner metadata', () => {
  afterEach(() => {
    $sessions.set([])
    $sessionTiles.set([])
  })

  it('keeps the tile source on its first optimistic sidebar row', () => {
    const storedSessionId = 'stored-tile-owner-metadata'
    const ownerRoute = { connectionId: 'source-a', profile: 'default' }
    $sessionTiles.set([{ ownerRoute, storedSessionId }])

    expect(
      listTileSessionRow({
        cwd: '/remote/worktree',
        model: 'model-a',
        preview: 'hello from the tile',
        runtimeId: 'rt-tile-owner-metadata',
        sessions: [],
        storedSessionId
      })
    ).toBe(true)

    expect($sessions.get()[0]).toMatchObject({
      connection_id: 'source-a',
      id: storedSessionId,
      profile: 'default'
    })
  })
})

// A tile's cancelRun/steerPrompt/reloadFromMessage each build their own
// requestGateway call directly instead of going through the shared
// submitPromptText pipeline (which already wraps its call in
// withSessionNotFoundResume) — see use-prompt-actions/index.test.tsx's
// "sleep/wake session recovery" suite for the same regression on the
// primary chat's own reloadFromMessage.
describe('useSessionTileActions sleep/wake session recovery', () => {
  beforeEach(() => {
    $activeSessionId.set('foreground-runtime')
    setSessions([])
    $sessionTiles.set([{ runtimeId: RUNTIME_SESSION_ID, storedSessionId: STORED_SESSION_ID }])
    setSessionTileDelegate({
      archiveSession: vi.fn(async () => undefined),
      branchSession: vi.fn(async () => undefined),
      deleteSession: vi.fn(async () => undefined),
      executeSlash: vi.fn(async () => true),
      interruptSession: vi.fn(async () => undefined),
      resumeTile: vi.fn(async () => RUNTIME_SESSION_ID),
      submitToSession: vi.fn(async () => undefined),
      updateSession: vi.fn((_runtimeId, updater) =>
        updater({
          attachedImages: [],
          busy: false,
          cwd: null,
          messages: [],
          model: null,
          streamId: null,
          storedSessionId: STORED_SESSION_ID
        } as never)
      )
    })
  })

  afterEach(() => {
    $activeSessionId.set(null)
    setSessions([])
    $sessionTiles.set([])
    requestGatewayMock.mockReset()
    vi.restoreAllMocks()
  })

  it('retains a queued slash when the tile delegate cannot execute it', async () => {
    const executeSlash = vi.fn(async () => false)
    const composerStorageScope = 'owner-qualified-queue-scope'

    setSessionTileDelegate({
      archiveSession: vi.fn(async () => undefined),
      branchSession: vi.fn(async () => undefined),
      deleteSession: vi.fn(async () => undefined),
      executeSlash,
      interruptSession: vi.fn(async () => undefined),
      resumeTile: vi.fn(async () => RUNTIME_SESSION_ID),
      submitToSession: vi.fn(async () => undefined),
      updateSession: vi.fn()
    } as never)

    const { result } = renderTileActions()

    await expect(
      result.current.submitText('/status', {
        composerStorageScope,
        fromQueue: true,
        sessionId: RUNTIME_SESSION_ID,
        storedSessionId: STORED_SESSION_ID
      })
    ).resolves.toBe(false)

    expect(executeSlash).toHaveBeenCalledWith('/status', RUNTIME_SESSION_ID, {
      composerStorageScope,
      fromQueue: true,
      storedSessionId: STORED_SESSION_ID
    })
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
    expect(calls[1]?.params).toMatchObject({ session_id: STORED_SESSION_ID, source: 'desktop', omit_messages: true })
    expect(calls[2]?.params).toEqual({ session_id: RECOVERED_SESSION_ID })
    expect($sessionTiles.get()[0]?.runtimeId).toBe(RECOVERED_SESSION_ID)
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

        return { status: 'redirected' }
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
    expect(calls[2]?.params).toEqual({ session_id: RECOVERED_SESSION_ID, text: 'actually use Postgres' })
    expect($sessionTiles.get()[0]?.runtimeId).toBe(RECOVERED_SESSION_ID)
  })

  it('rebinds prompt.submit recovery to the tile without changing the foreground session', async () => {
    const calls: { method: string; params?: Record<string, unknown> }[] = []
    let submitAttempts = 0

    requestGatewayMock.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      calls.push({ method, params })

      if (method === 'prompt.submit') {
        submitAttempts += 1

        if (submitAttempts === 1) {
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
      await expect(result.current.submitText('continue the bot chat')).resolves.toBe(true)
    })

    expect(calls.map(c => c.method)).toEqual(['prompt.submit', 'session.resume', 'prompt.submit'])
    expect(calls[0]?.params).toMatchObject({ session_id: RUNTIME_SESSION_ID })
    expect(calls[1]?.params).toMatchObject({ session_id: STORED_SESSION_ID, source: 'desktop', omit_messages: true })
    expect(calls[2]?.params).toMatchObject({ session_id: RECOVERED_SESSION_ID })
    expect($sessionTiles.get()[0]?.runtimeId).toBe(RECOVERED_SESSION_ID)
    expect($activeSessionId.get()).toBe('foreground-runtime')
  })

  it('keeps duplicate-owner interrupt and attachment recoveries in separate canonical flights', async () => {
    const ownerA: SessionOwnerRoute = { connectionId: 'source-a', mode: 'remote', profile: 'worker' }
    const ownerB: SessionOwnerRoute = { connectionId: 'source-b', mode: 'remote', profile: 'worker' }
    let releaseOwnerA!: () => void

    const ownerAResumeGate = new Promise<void>(resolve => {
      releaseOwnerA = resolve
    })

    setSessions([
      { connection_id: ownerA.connectionId, id: STORED_SESSION_ID, profile: ownerA.profile } as never,
      { connection_id: ownerB.connectionId, id: STORED_SESSION_ID, profile: ownerB.profile } as never
    ])
    $sessionTiles.set([
      { ownerRoute: ownerA, runtimeId: 'rt-dead-a', storedSessionId: STORED_SESSION_ID },
      { ownerRoute: ownerB, runtimeId: 'rt-dead-b', storedSessionId: STORED_SESSION_ID }
    ])

    vi.mocked(requestGatewayForAgent).mockImplementation(async (connectionId, _profile, method, params) => {
      if (method === 'session.interrupt' && params?.session_id === 'rt-dead-a') {
        throw new Error('session not found')
      }

      if (method === 'image.attach_bytes' && params?.session_id === 'rt-dead-b') {
        throw new Error('session not found')
      }

      if (method === 'session.resume') {
        if (connectionId === ownerA.connectionId) {
          await ownerAResumeGate

          return { session_id: 'rt-owner-a' } as never
        }

        return { session_id: 'rt-owner-b' } as never
      }

      if (method === 'image.attach_bytes') {
        return { attached: true, path: '/remote/photo.png' } as never
      }

      return {} as never
    })

    const scopeB: ComposerScope = {
      ...MAIN_COMPOSER_SCOPE,
      attachments: createComposerAttachmentScope(),
      target: `tile:${STORED_SESSION_ID}:owner-b`
    }

    scopeB.attachments.add({
      id: 'image:owner-b',
      kind: 'image',
      label: 'photo.png',
      path: '/client/photo.png',
      previewUrl: 'data:image/png;base64,aGVsbG8='
    })

    const ownerAHook = renderTileActions({ ownerRoute: ownerA, runtimeId: 'rt-dead-a' })
    const ownerBHook = renderTileActions({ ownerRoute: ownerB, runtimeId: 'rt-dead-b', scope: scopeB })

    let cancelA!: Promise<void>
    act(() => {
      cancelA = ownerAHook.result.current.cancelRun()
    })

    await vi.waitFor(() =>
      expect(requestGatewayForAgent).toHaveBeenCalledWith(
        ownerA.connectionId,
        ownerA.profile,
        'session.resume',
        expect.objectContaining({ session_id: STORED_SESSION_ID })
      )
    )

    let submitB!: Promise<boolean>
    act(() => {
      submitB = ownerBHook.result.current.submitText('inspect owner B image')
    })

    await Promise.resolve()
    releaseOwnerA()
    await act(async () => {
      await Promise.all([cancelA, submitB])
    })

    const resumeCalls = vi
      .mocked(requestGatewayForAgent)
      .mock.calls.filter(([, , method]) => method === 'session.resume')

    expect(resumeCalls).toHaveLength(2)
    expect(resumeCalls.map(([connectionId]) => connectionId)).toEqual(
      expect.arrayContaining([ownerA.connectionId, ownerB.connectionId])
    )
    expect(requestGatewayForAgent).toHaveBeenCalledWith(
      ownerB.connectionId,
      ownerB.profile,
      'image.attach_bytes',
      expect.objectContaining({ session_id: 'rt-owner-b' })
    )
  })
})
