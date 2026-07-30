import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  $attentionSessionIds,
  $stalledSessionIds,
  $workingSessionIds,
  clearAllSessionStates,
  SESSION_WATCHDOG_TIMEOUT_MS
} from '@/store/session-states'
import { $subagentsBySession } from '@/store/subagents'

import { rehydrateLiveSessionStatuses, useBackgroundSync } from './use-background-sync'

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

describe('useBackgroundSync subagent reconciliation', () => {
  afterEach(() => {
    $subagentsBySession.set({})
  })

  it('requests and applies the active subagent snapshot when the gateway opens', async () => {
    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.active_list') {
        return { sessions: [] }
      }

      if (method === 'delegation.status') {
        return {
          active: [
            {
              goal: 'visible after reconnect',
              origin_ui_session_id: 'runtime-reconnected',
              status: 'running',
              subagent_id: 'sa-reconnected'
            }
          ]
        }
      }

      return {}
    })

    const refresh = vi.fn(async () => undefined)

    const { unmount } = renderHook(() =>
      useBackgroundSync({
        activeGatewayProfile: 'reconnect-test',
        activeIsMessaging: false,
        activeSessionId: null,
        freshDraftReady: false,
        gatewayState: 'open',
        refreshActiveMessagingTranscript: refresh,
        refreshCronJobs: refresh,
        refreshCurrentModel: refresh,
        refreshHermesConfig: refresh,
        refreshMessagingSessions: refresh,
        refreshSessions: refresh,
        requestGateway: requestGateway as never
      })
    )

    await waitFor(() => {
      expect(requestGateway).toHaveBeenCalledWith('delegation.status', { profile: 'reconnect-test' })
      expect($subagentsBySession.get()['runtime-reconnected']?.[0]?.id).toBe('sa-reconnected')
    })

    unmount()
  })
})
