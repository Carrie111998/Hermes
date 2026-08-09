import { act, render } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesConnection } from '@/global'
import {
  $attentionSessionIds,
  $stalledSessionIds,
  $workingSessionIds,
  clearAllSessionStates,
  SESSION_WATCHDOG_TIMEOUT_MS
} from '@/store/session-states'
import { $connection } from '@/store/session'

import { rehydrateLiveSessionStatuses, useBackgroundSync } from './use-background-sync'

function connection(mode: HermesConnection['mode']): HermesConnection {
  return {
    baseUrl: mode === 'remote' ? 'https://remote.example' : '',
    isFullscreen: false,
    logs: [],
    mode,
    nativeOverlayWidth: 0,
    profile: 'default',
    token: '',
    windowButtonPosition: null,
    wsUrl: ''
  }
}

function BackgroundSyncHarness({ refreshSessions }: { refreshSessions: () => void }) {
  useBackgroundSync({
    activeGatewayProfile: 'default',
    activeIsMessaging: false,
    activeSessionId: null,
    freshDraftReady: true,
    gatewayState: 'open',
    refreshActiveMessagingTranscript: () => undefined,
    refreshCronJobs: () => undefined,
    refreshCurrentModel: () => undefined,
    refreshHermesConfig: () => undefined,
    refreshMessagingSessions: () => undefined,
    refreshSessions,
    requestGateway: async <T>() => ({ sessions: [] }) as T
  })

  return null
}

describe('rehydrateLiveSessionStatuses', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.clearAllTimers()
    vi.useRealTimers()
    clearAllSessionStates()
  })

  it('restores running sessions after reconnect without opening them', () => {
    const now = 1_800_000_000_000

    rehydrateLiveSessionStatuses(
      {
        sessions: [
          {
            id: 'runtime-overnight',
            last_active: (now - SESSION_WATCHDOG_TIMEOUT_MS - 1_000) / 1000,
            session_key: 'overnight-exam-learning',
            status: 'working'
          },
          {
            id: 'runtime-cleanup',
            last_active: now / 1000,
            session_key: 'temporary-file-cleanup',
            status: 'working'
          }
        ]
      },
      now
    )

    expect($workingSessionIds.get()).toEqual(['overnight-exam-learning', 'temporary-file-cleanup'])
    expect($stalledSessionIds.get()).toEqual(['overnight-exam-learning'])
    expect($attentionSessionIds.get()).toEqual([])
  })

  it('restores a waiting turn as working and needing attention', () => {
    rehydrateLiveSessionStatuses({
      sessions: [{ id: 'runtime-needs-user', session_key: 'needs-user', status: 'waiting' }]
    })

    expect($workingSessionIds.get()).toEqual(['needs-user'])
    expect($attentionSessionIds.get()).toEqual(['needs-user'])
    expect($stalledSessionIds.get()).toEqual([])
  })

  it('ignores idle, starting, and malformed live-session rows', () => {
    rehydrateLiveSessionStatuses({
      sessions: [
        { id: 'runtime-idle', session_key: 'idle-session', status: 'idle' },
        { id: 'runtime-starting', session_key: 'starting-session', status: 'starting' },
        { id: 'runtime-malformed', status: 'working' }
      ]
    })

    expect($workingSessionIds.get()).toEqual([])
    expect($attentionSessionIds.get()).toEqual([])
    expect($stalledSessionIds.get()).toEqual([])
  })
})

describe('useBackgroundSync', () => {
  it('refreshes the sidebar when the active backend mode changes', async () => {
    const refreshSessions = vi.fn()

    $connection.set(connection('local'))
    const view = render(createElement(BackgroundSyncHarness, { refreshSessions }))

    expect(refreshSessions).toHaveBeenCalledTimes(1)

    await act(async () => {
      $connection.set(connection('remote'))
    })

    expect(refreshSessions).toHaveBeenCalledTimes(2)
    view.unmount()
  })
})
