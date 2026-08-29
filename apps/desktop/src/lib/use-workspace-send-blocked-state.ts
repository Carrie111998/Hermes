import { useStoresSelector } from '@/lib/use-session-slice'
import {
  blockedWorkspaceSendState,
  collectWorkspaceSendInput,
  type WorkspaceSendBlockedState
} from '@/lib/workspace-send-gate'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $pendingConnectionId } from '@/store/connections'
import { $gatewaySwitching } from '@/store/gateway-switch'
import { $activeGatewayProfile, $profiles } from '@/store/profile'
import {
  $connection,
  $cronSessions,
  $gatewayState,
  $messagingSessions,
  $sessionOwnerHintsVersion,
  $sessions
} from '@/store/session'
import { $botChatSessionIds, $sessionStates, $sessionTiles } from '@/store/session-states'

/** Reactive, referentially-stable send barrier for composer surfaces.
 *
 * Keep every store read by collectWorkspaceSendInput (including its owner and
 * sole-topology helpers) in this list. The selector deliberately returns a
 * scalar: useSyncExternalStore snapshots may not allocate a verdict object on
 * each read or React's tearing check can enter a maximum-update-depth loop.
 */
export function useWorkspaceSendBlockedState(
  sessionId: null | string | undefined,
  storedSessionId: null | string | undefined
): null | WorkspaceSendBlockedState {
  return useStoresSelector(
    [
      $activeGatewayProfile,
      $botChatSessionIds,
      $connection,
      $connectionsRegistry,
      $cronSessions,
      $gatewayState,
      $gatewaySwitching,
      $messagingSessions,
      $pendingConnectionId,
      $profiles,
      $sessionOwnerHintsVersion,
      $sessions,
      $sessionStates,
      $sessionTiles
    ],
    () => blockedWorkspaceSendState(collectWorkspaceSendInput({ sessionId, storedSessionId }))
  )
}
