import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $activeGatewayProfile } from '@/store/profile'
import { setSessionTileDelegate } from '@/store/session-states'
import { routeWakeVoiceTranscript } from '@/store/wake-voice-routing'

import { useWakeVoiceRoutingBridge } from './use-wake-voice-routing-bridge'

vi.mock('@/i18n', () => ({
  translateNow: (key: string, value?: string) => `${key}:${value ?? ''}`
}))

const { listAllProfileSessions, notify, notifyError } = vi.hoisted(() => ({
  listAllProfileSessions: vi.fn(),
  notify: vi.fn(),
  notifyError: vi.fn()
}))

vi.mock('@/hermes', () => ({ listAllProfileSessions, setApiRequestProfile: vi.fn() }))
vi.mock('@/store/notifications', () => ({ notify, notifyError }))
vi.mock('@/store/windows', () => ({ isSecondaryWindow: () => false }))

const resumeTile = vi.fn(async (storedSessionId: string) => `runtime-${storedSessionId}`)
const submitToSession = vi.fn(async () => undefined)

function installDelegate() {
  setSessionTileDelegate({
    archiveSession: vi.fn(),
    branchSession: vi.fn(),
    deleteSession: vi.fn(),
    executeSlash: vi.fn(),
    interruptSession: vi.fn(),
    resumeTile,
    submitToSession,
    updateSession: vi.fn()
  })
}

describe('useWakeVoiceRoutingBridge', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    $activeGatewayProfile.set('default')
    listAllProfileSessions.mockResolvedValue({
      limit: 500,
      offset: 0,
      profile_totals: { default: 3 },
      sessions: [
        { archived: false, id: 'toss', profile: 'default', title: 'Toss résumé' },
        { archived: false, id: 'gmail', profile: 'default', title: 'Gmail integration' },
        { archived: false, id: 'gmail-tests', profile: 'default', title: 'Gmail integration tests' }
      ],
      total: 3
    })
    installDelegate()
  })

  afterEach(cleanup)

  it('resumes and submits to the resolved session without changing the primary session', async () => {
    renderHook(() => useWakeVoiceRoutingBridge())

    await expect(
      act(() => routeWakeVoiceTranscript('send to Toss resume session: check the metrics', 'default'))
    ).resolves.toBe('routed')
    expect(listAllProfileSessions).toHaveBeenCalledWith(500, 1, 'exclude', 'recent', 'default')
    expect(resumeTile).toHaveBeenCalledWith('toss', 'default')
    expect(submitToSession).toHaveBeenCalledWith('runtime-toss', 'check the metrics')
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({ kind: 'success', message: 'notifications.voice.routedToSession:Toss résumé' })
    )
  })

  it('rejects ambiguous matches without resuming or submitting', async () => {
    renderHook(() => useWakeVoiceRoutingBridge())

    await expect(
      act(() => routeWakeVoiceTranscript('send to Gmail session: rerun it', 'default'))
    ).resolves.toBe('rejected')
    expect(resumeTile).not.toHaveBeenCalled()
    expect(submitToSession).not.toHaveBeenCalled()
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({
        detail: 'Gmail integration\nGmail integration tests',
        kind: 'warning'
      })
    )
  })

  it('passes ordinary first turns through without listing sessions', async () => {
    renderHook(() => useWakeVoiceRoutingBridge())

    await expect(act(() => routeWakeVoiceTranscript('review the current draft', 'default'))).resolves.toBe(
      'not-route'
    )
    expect(listAllProfileSessions).not.toHaveBeenCalled()
    expect(resumeTile).not.toHaveBeenCalled()
    expect(submitToSession).not.toHaveBeenCalled()
  })

  it('reports transport failure and never claims the route succeeded', async () => {
    resumeTile.mockRejectedValueOnce(new Error('session unavailable'))
    renderHook(() => useWakeVoiceRoutingBridge())

    await expect(
      act(() => routeWakeVoiceTranscript('send to Toss resume session: check the metrics', 'default'))
    ).resolves.toBe('rejected')
    expect(submitToSession).not.toHaveBeenCalled()
    expect(notifyError).toHaveBeenCalled()
  })

  it('uses the profile latched by the wake event instead of a later active profile', async () => {
    $activeGatewayProfile.set('default')
    listAllProfileSessions.mockResolvedValueOnce({
      limit: 500,
      offset: 0,
      profile_totals: { work: 1 },
      sessions: [{ archived: false, id: 'work-toss', profile: 'work', title: 'Toss résumé' }],
      total: 1
    })
    renderHook(() => useWakeVoiceRoutingBridge())

    await expect(
      act(() => routeWakeVoiceTranscript('send to Toss resume session: check the metrics', 'work'))
    ).resolves.toBe('routed')
    expect(listAllProfileSessions).toHaveBeenCalledWith(500, 1, 'exclude', 'recent', 'work')
    expect(resumeTile).toHaveBeenCalledWith('work-toss', 'work')
  })

  it('fails closed when the requested profile listing is truncated', async () => {
    listAllProfileSessions.mockResolvedValueOnce({
      limit: 500,
      offset: 0,
      profile_totals: { work: 501 },
      sessions: [{ archived: false, id: 'toss', profile: 'work', title: 'Toss résumé' }],
      total: 501
    })
    renderHook(() => useWakeVoiceRoutingBridge())

    await expect(
      act(() => routeWakeVoiceTranscript('send to Toss resume session: check the metrics', 'work'))
    ).resolves.toBe('rejected')
    expect(resumeTile).not.toHaveBeenCalled()
    expect(submitToSession).not.toHaveBeenCalled()
  })
})
