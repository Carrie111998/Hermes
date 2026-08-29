import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $pendingConnectionId } from '@/store/connections'
import { $gatewaySwitching } from '@/store/gateway-switch'
import { $activeGatewayProfile, $profiles } from '@/store/profile'
import { $connection, $gatewayState, _resetSessionOwnerHintsForTests } from '@/store/session'
import { $botChatSessionIds, $sessionStates, $sessionTiles, clearAllSessionStates } from '@/store/session-states'

import { useWorkspaceSendBlockedState } from './use-workspace-send-blocked-state'

describe('useWorkspaceSendBlockedState', () => {
  beforeEach(() => {
    $activeGatewayProfile.set('default')
    $botChatSessionIds.set(new Set())
    $connection.set({ connectionId: 'local', mode: 'local' } as never)
    $connectionsRegistry.set({
      connections: [
        { id: 'local', kind: 'local', label: 'This device' },
        { id: 'remote', kind: 'remote', label: 'Remote', url: 'https://remote.test' }
      ],
      lastUsed: 'local',
      launchMode: 'last-used',
      primary: 'local',
      version: 2
    } as never)
    $gatewayState.set('open')
    $gatewaySwitching.set(false)
    $pendingConnectionId.set(null)
    $profiles.set([{ is_default: true, name: 'default' }] as never)
    $sessionStates.set({})
    $sessionTiles.set([
      {
        ownerRoute: { connectionId: 'local', mode: 'local', profile: 'default' },
        runtimeId: 'runtime-a',
        storedSessionId: 'stored-a'
      }
    ])
  })

  afterEach(() => {
    cleanup()
    clearAllSessionStates()
    _resetSessionOwnerHintsForTests()
    $connectionsRegistry.set(null)
    $connection.set(null)
    $gatewayState.set('idle')
    $profiles.set([])
  })

  it('mounts with a stable scalar snapshot and reacts to tile-only owner changes', () => {
    let renders = 0

    const { result } = renderHook(() => {
      renders += 1

      return useWorkspaceSendBlockedState('runtime-a', 'stored-a')
    })

    expect(result.current).toBeNull()
    expect(renders).toBeLessThan(4)

    act(() => {
      $sessionTiles.set([
        {
          ownerRoute: { connectionId: 'remote', mode: 'remote', profile: 'default' },
          runtimeId: 'runtime-a',
          storedSessionId: 'stored-a'
        }
      ])
    })

    expect(result.current).toBe('route_invalid')
    expect(renders).toBeLessThan(6)
  })
})
