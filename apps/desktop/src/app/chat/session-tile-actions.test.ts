import { renderHook } from '@testing-library/react'
import { act } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { MAIN_COMPOSER_SCOPE } from './composer/scope'

const requestGatewayMock = vi.hoisted(() => vi.fn())

const { $activeSessionId, $sessions, setSessions } = await import('@/store/session')
const { $sessionTiles, setSessionTileDelegate } = await import('@/store/session-states')
const { listTileSessionRow, useSessionTileActions } = await import('./session-tile-actions')

const RUNTIME_SESSION_ID = 'rt-tile-current'
const STORED_SESSION_ID = 'stored-tile-db'
const RECOVERED_SESSION_ID = 'rt-tile-recovered'

function renderTileActions() {
  return renderHook(() =>
    useSessionTileActions({
      requestGateway: requestGatewayMock,
      runtimeId: RUNTIME_SESSION_ID,
      scope: MAIN_COMPOSER_SCOPE,
      storedSessionId: STORED_SESSION_ID
    })
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
      executeSlash: vi.fn(async () => undefined),
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

  /**
   * The chip is a summary; the payload is the thing.
   *
   * `pinCommentBlock` and `reviewCommentBlock` were unit-tested and green
   * while the send path joined `a.refText` raw, so the model received the
   * literal string "1 preview comment" and could only ask what was meant.
   * This asserts the WIRE text, which is the only place that failure was
   * visible — a pure-function test cannot see it.
   */
  it('sends a preview-pin attachment as its comments block, not its chip text', async () => {
    const calls: { method: string; params?: Record<string, unknown> }[] = []

    requestGatewayMock.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      calls.push({ method, params })

      return {}
    })

    const pins = [
      {
        anchor: { label: 'Capacity and reach', path: 'body>main>h1', role: 'heading', selector: 'h1.hero-title' },
        comment: 'this heading is too close to the top bar',
        createdAt: 1,
        id: 'pin-1',
        kind: 'element',
        pageUrl: 'http://127.0.0.1:8080/en/about/numbers/',
        resolved: false
      }
    ]

    const { result } = renderTileActions()

    await act(async () => {
      await result.current.submitText('', {
        attachments: [
          {
            detail: JSON.stringify(pins),
            id: 'pins:pin-1',
            kind: 'pins',
            label: '1 comment',
            refText: '1 preview comment'
          }
        ]
      } as never)
    })

    const text = String(calls.find(c => c.method === 'prompt.submit')?.params?.text ?? '')

    expect(text).toContain('```preview-comments')
    // The three things the user could not act without: which page, which
    // element, and what they said about it.
    expect(text).toContain('http://127.0.0.1:8080/en/about/numbers/')
    expect(text).toContain('h1.hero-title')
    expect(text).toContain('this heading is too close to the top bar')
  })
})
