import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $changeEventsAvailable, $cronChangeTick, $sessionsChangeTick } from '@/store/live-sync'
import { $activeSessionId } from '@/store/session'

import { useBackgroundSync } from './use-background-sync'

const noop = () => undefined
const requestGateway = async () => ({ sessions: [] })

function render(
  activeGatewayProfile: string,
  activeConnectionId: string,
  refreshSessions: () => Promise<void>,
  options: {
    freshDraftReady?: boolean
    refreshCurrentModel?: () => Promise<void> | void
    refreshHermesConfig?: () => Promise<void> | void
  } = {}
) {
  const {
    freshDraftReady = false,
    refreshCurrentModel = noop,
    refreshHermesConfig = noop
  } = options

  return renderHook(
    ({ connectionId, freshDraft, profile }: { connectionId: string; freshDraft: boolean; profile: string }) => {
      useBackgroundSync({
        activeConnectionId: connectionId,
        activeGatewayProfile: profile,
        activeIsMessaging: false,
        activeSessionId: null,
        activeStoredSessionId: null,
        freshDraftReady: freshDraft,
        gatewayState: 'open',
        refreshActiveTranscript: noop,
        refreshCronJobs: noop,
        refreshCurrentModel,
        refreshHermesConfig,
        refreshMessagingSessions: noop,
        refreshSessions,
        requestGateway
      })
    },
    { initialProps: { connectionId: activeConnectionId, freshDraft: freshDraftReady, profile: activeGatewayProfile } }
  )
}

describe('useBackgroundSync profile-scoped session refresh', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    $activeSessionId.set(null)
    $changeEventsAvailable.set(false)
    $cronChangeTick.set(0)
    $sessionsChangeTick.set(0)
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('does not re-fetch profile defaults when a sticky fresh draft opens', async () => {
    const refreshSessions = vi.fn(async () => undefined)
    const refreshCurrentModel = vi.fn(async () => undefined)
    const refreshHermesConfig = vi.fn(async () => undefined)

    const hook = render('default', 'local', refreshSessions, {
      refreshCurrentModel,
      refreshHermesConfig
    })

    await act(async () => undefined)
    refreshCurrentModel.mockClear()
    refreshHermesConfig.mockClear()

    hook.rerender({ connectionId: 'local', freshDraft: true, profile: 'default' })

    await act(async () => undefined)
    expect(refreshCurrentModel).not.toHaveBeenCalled()
    expect(refreshHermesConfig).not.toHaveBeenCalled()
  })

  it('refreshes the session list after the active gateway profile changes', async () => {
    const refreshSessions = vi.fn(async () => undefined)
    const hook = render('default', 'local', refreshSessions)

    await act(async () => undefined)
    expect(refreshSessions).toHaveBeenCalledTimes(1)
    refreshSessions.mockClear()

    hook.rerender({ connectionId: 'local', freshDraft: false, profile: 'nova' })

    await act(async () => undefined)
    expect(refreshSessions).toHaveBeenCalledTimes(1)
  })

  it('refreshes the session list when the backend changes but the profile name does not', async () => {
    const refreshSessions = vi.fn(async () => undefined)
    const hook = render('default', 'work', refreshSessions)

    await act(async () => undefined)
    refreshSessions.mockClear()

    hook.rerender({ connectionId: 'homelab', freshDraft: false, profile: 'default' })

    await act(async () => undefined)
    expect(refreshSessions).toHaveBeenCalledTimes(1)
  })
})
