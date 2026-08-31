/** Existing Sessions/Bot send-surface states. Fail closed unless a state allows send. */
import { LOCAL_CONNECTION_ID } from '@hermes/shared'

import { $pendingConnectionId } from '@/store/connections'
import { $gatewaySwitching, currentGatewaySwitchGeneration } from '@/store/gateway-switch'
import { $activeGatewayProfile } from '@/store/profile'
import { $connection, $gatewayState } from '@/store/session'
import { ambientGatewayOwnsEverySession } from '@/store/session-owner-resolution'
import { isSessionOwnerRoute, type SessionOwnerScope } from '@/store/session-request-router'
import { isBotChatSession, knownOwnerForSession } from '@/store/session-states'

export const WORKSPACE_SEND_SURFACE_STATES = [
  'already_active',
  'auth_required',
  'bot_talk_across',
  'idle_fleet',
  'route_invalid',
  'switch_failed',
  'switching',
  'unreachable',
  'unsupported_build'
] as const

export type WorkspaceSendSurfaceState = (typeof WORKSPACE_SEND_SURFACE_STATES)[number]
export type WorkspaceSendAllowedState = Extract<
  WorkspaceSendSurfaceState,
  'already_active' | 'bot_talk_across' | 'idle_fleet'
>
export type WorkspaceSendBlockedState = Exclude<WorkspaceSendSurfaceState, WorkspaceSendAllowedState>

export type WorkspaceSendTuple = {
  connectionId: string
  profile: string
}

export type WorkspaceSendInput = {
  capturedGeneration?: number
  currentGeneration?: number
  gatewaySwitching: boolean
  pendingConnectionId: string | null
  authRequired?: boolean
  botTalkAcross?: boolean
  intendedOwner?: null | WorkspaceSendTuple
  isNewChat?: boolean
  ambientTupleValid?: boolean
  readinessResolved?: boolean
  socketOwner?: null | WorkspaceSendTuple
  switchFailed?: boolean
  unreachable?: boolean
  unsupportedBuild?: boolean
}

export type WorkspaceSendVerdict =
  { allowed: true; state: WorkspaceSendAllowedState } | { allowed: false; state: WorkspaceSendBlockedState }

export type WorkspaceSubmitResult =
  { ok: true; reason?: WorkspaceSendSurfaceState } | { ok: false; reason: WorkspaceSendSurfaceState }

export type WorkspaceSubmitOutcome = WorkspaceSubmitResult | boolean

function tuplesEqual(left?: null | WorkspaceSendTuple, right?: null | WorkspaceSendTuple): boolean {
  return Boolean(left && right && left.connectionId === right.connectionId && left.profile === right.profile)
}

function tupleFromOwner(owner: SessionOwnerScope, socketOwner: null | WorkspaceSendTuple): null | WorkspaceSendTuple {
  if (isSessionOwnerRoute(owner)) {
    const connectionId = owner.connectionId.trim()
    const profile = owner.profile.trim()

    return connectionId && profile ? { connectionId, profile } : null
  }

  const profile = typeof owner === 'string' ? owner.trim() : ''

  if (!profile) {
    return null
  }

  const connectionId = socketOwner?.connectionId ?? (ambientGatewayOwnsEverySession() ? LOCAL_CONNECTION_ID : '')

  return connectionId ? { connectionId, profile } : null
}

/** Live Sessions/Bot send input. Fail closed when owner/tuple/readiness is missing. */
export function collectWorkspaceSendInput(input: {
  capturedGeneration?: number
  sessionId?: null | string
  storedSessionId?: null | string
}): WorkspaceSendInput {
  const connectionId = String($connection.get()?.connectionId ?? '').trim()
  const profile = String($activeGatewayProfile.get() ?? '').trim()

  const socketOwner: null | WorkspaceSendTuple =
    connectionId && profile
      ? { connectionId, profile }
      : ambientGatewayOwnsEverySession() && profile
        ? { connectionId: LOCAL_CONNECTION_ID, profile }
        : null

  const storedSessionId = input.storedSessionId || null
  const sessionId = input.sessionId || null
  const isNewChat = !storedSessionId && !sessionId
  const owner = knownOwnerForSession(storedSessionId) ?? knownOwnerForSession(sessionId)
  const intendedFromOwner = tupleFromOwner(owner, socketOwner)

  const intendedOwner = isNewChat
    ? undefined
    : ambientGatewayOwnsEverySession()
      ? socketOwner
      : (intendedFromOwner ?? null)

  const gatewayState = $gatewayState.get()
  const botTalkAcross = Boolean(!isNewChat && (isBotChatSession(sessionId) || isBotChatSession(storedSessionId)))

  return {
    ambientTupleValid: Boolean(socketOwner?.connectionId && socketOwner?.profile),
    botTalkAcross,
    capturedGeneration: input.capturedGeneration,
    currentGeneration: input.capturedGeneration === undefined ? undefined : currentGatewaySwitchGeneration(),
    gatewaySwitching: $gatewaySwitching.get(),
    intendedOwner,
    isNewChat,
    pendingConnectionId: $pendingConnectionId.get(),
    socketOwner,
    ...(gatewayState === 'open'
      ? { readinessResolved: true }
      : gatewayState === 'idle'
        ? {}
        : { readinessResolved: false }),
    ...(gatewayState === 'error' ? { unreachable: true } : {})
  }
}

export function evaluateWorkspaceSend(input: WorkspaceSendInput): WorkspaceSendVerdict {
  if (
    input.pendingConnectionId != null ||
    input.gatewaySwitching ||
    (input.capturedGeneration !== undefined &&
      input.currentGeneration !== undefined &&
      input.capturedGeneration !== input.currentGeneration)
  ) {
    return { allowed: false, state: 'switching' }
  }

  if (input.switchFailed) {
    return { allowed: false, state: 'switch_failed' }
  }

  if (input.unsupportedBuild) {
    return { allowed: false, state: 'unsupported_build' }
  }

  if (input.unreachable) {
    return { allowed: false, state: 'unreachable' }
  }

  if (input.authRequired || input.readinessResolved === false) {
    return { allowed: false, state: 'auth_required' }
  }

  if (input.botTalkAcross) {
    return { allowed: true, state: 'bot_talk_across' }
  }

  if (input.isNewChat === false && !input.intendedOwner) {
    return { allowed: false, state: 'route_invalid' }
  }

  if (input.intendedOwner && input.socketOwner && !tuplesEqual(input.intendedOwner, input.socketOwner)) {
    return { allowed: false, state: 'route_invalid' }
  }

  if (input.intendedOwner && input.socketOwner && tuplesEqual(input.intendedOwner, input.socketOwner)) {
    return { allowed: true, state: 'already_active' }
  }

  if (input.isNewChat && input.ambientTupleValid) {
    return { allowed: true, state: 'idle_fleet' }
  }

  return { allowed: false, state: 'route_invalid' }
}

/** Stable scalar for useSyncExternalStore selectors. Returning the verdict
 * object itself would allocate a new snapshot on every read and can trigger a
 * React maximum-update-depth loop. */
export function blockedWorkspaceSendState(input: WorkspaceSendInput): null | WorkspaceSendBlockedState {
  const verdict = evaluateWorkspaceSend(input)

  return verdict.allowed ? null : verdict.state
}

/** Sessions-switch send barrier plus the fail-closed existing-surface set. */
export function isWorkspaceSendBlocked(input: WorkspaceSendInput): boolean {
  return !evaluateWorkspaceSend(input).allowed
}

export function isSessionsSwitchInFlight(input: {
  gatewaySwitching: boolean
  pendingConnectionId: string | null
}): boolean {
  return evaluateWorkspaceSend({ ...input }).state === 'switching'
}

export function isSubmitDeferred(result: WorkspaceSubmitResult | boolean): boolean {
  // Every typed workspace rejection is transient routing/readiness state, not
  // a failed queued turn. Keep the entry in place without burning bounded
  // auto-drain attempts; a plain false remains a real submission failure.
  return typeof result === 'object' && result.ok === false
}

export function isSubmitAccepted(result: WorkspaceSubmitResult | boolean): boolean {
  return result === true || (typeof result === 'object' && result.ok === true)
}
