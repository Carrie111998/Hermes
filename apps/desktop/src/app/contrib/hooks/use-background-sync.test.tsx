import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { $changeEventsAvailable, $cronChangeTick, $sessionsChangeTick } from '@/store/live-sync'
import { $activeSessionId } from '@/store/session'
import { $sessionStates, clearAllSessionStates, publishSessionState } from '@/store/session-states'

import { useBackgroundSync } from './use-background-sync'

const noop = () => undefined
const defaultRequestGateway = async () => ({ sessions: [] })

function render(
  activeGatewayProfile: string,
  activeConnectionId: string,
  refreshSessions: () => Promise<void>,
  requestGateway: Parameters<typeof useBackgroundSync>[0]['requestGateway'] = defaultRequestGateway
) {
  return renderHook(
    ({ connectionId, profile }: { connectionId: string; profile: string }) => {
      useBackgroundSync({
        activeConnectionId: connectionId,
        activeGatewayProfile: profile,
        activeIsMessaging: false,
        activeSessionId: null,
        activeStoredSessionId: null,
        freshDraftReady: false,
        gatewayState: 'open',
        refreshActiveTranscript: noop,
        refreshCronJobs: noop,
        refreshCurrentModel: noop,
        refreshHermesConfig: noop,
        refreshMessagingSessions: noop,
        refreshSessions,
        requestGateway,
        sessionStateHasOwner: () => true,
        updateOwnedSessionState: () => true
      })
    },
    { initialProps: { connectionId: activeConnectionId, profile: activeGatewayProfile } }
  )
}

describe('useBackgroundSync profile-scoped session refresh', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    $activeSessionId.set(null)
    $changeEventsAvailable.set(false)
    $cronChangeTick.set(0)
    $sessionsChangeTick.set(0)
    clearAllSessionStates()
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('refreshes the session list after the active gateway profile changes', async () => {
    const refreshSessions = vi.fn(async () => undefined)
    const hook = render('default', 'local', refreshSessions)

    await act(async () => undefined)
    expect(refreshSessions).toHaveBeenCalledTimes(1)
    refreshSessions.mockClear()

    hook.rerender({ connectionId: 'local', profile: 'nova' })

    await act(async () => undefined)
    expect(refreshSessions).toHaveBeenCalledTimes(1)
  })

  it('refreshes the session list when the backend changes but the profile name does not', async () => {
    const refreshSessions = vi.fn(async () => undefined)
    const hook = render('default', 'work', refreshSessions)

    await act(async () => undefined)
    refreshSessions.mockClear()

    hook.rerender({ connectionId: 'homelab', profile: 'default' })

    await act(async () => undefined)
    expect(refreshSessions).toHaveBeenCalledTimes(1)
  })

  it('drops a delayed active_list response when the same profile switches connections', async () => {
    let resolveConnectionA: ((value: unknown) => void) | undefined

    const requestGateway = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise(resolve => {
            resolveConnectionA = resolve
          })
      )
      .mockResolvedValue({ sessions: [] })

    const hook = render(
      'default',
      'connection-a',
      vi.fn(async () => undefined),
      requestGateway
    )

    await act(async () => undefined)
    hook.rerender({ connectionId: 'connection-b', profile: 'default' })
    await act(async () => undefined)

    const ownerB = {
      ...createClientSessionState('stored-shared'),
      awaitingResponse: true,
      busy: true,
      connectionId: 'connection-b',
      profile: 'default'
    }

    publishSessionState('runtime-shared', ownerB)

    await act(async () => {
      resolveConnectionA?.({
        sessions: [{ id: 'runtime-shared', session_key: 'stored-shared', status: 'working' }]
      })
      await Promise.resolve()
    })

    expect(requestGateway).toHaveBeenCalledTimes(2)
    expect($sessionStates.get()['runtime-shared']).toBe(ownerB)
  })
})
