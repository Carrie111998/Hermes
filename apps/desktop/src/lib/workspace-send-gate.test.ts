import { afterEach, describe, expect, it } from 'vitest'

import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $pendingConnectionId } from '@/store/connections'
import { $gatewaySwitching, endGatewaySwitch } from '@/store/gateway-switch'
import { $connection, $gatewayState } from '@/store/session'
import { clearAllSessionStates } from '@/store/session-states'

import {
  blockedWorkspaceSendState,
  collectWorkspaceSendInput,
  evaluateWorkspaceSend,
  isSubmitAccepted,
  isSubmitDeferred,
  isWorkspaceSendBlocked,
  WORKSPACE_SEND_SURFACE_STATES,
  type WorkspaceSendSurfaceState
} from './workspace-send-gate'

const idle = {
  gatewaySwitching: false,
  pendingConnectionId: null as string | null
}

describe('workspace send surface states', () => {
  it('exposes the complete typed existing-surface set', () => {
    expect([...WORKSPACE_SEND_SURFACE_STATES].sort()).toEqual(
      [
        'already_active',
        'auth_required',
        'bot_talk_across',
        'idle_fleet',
        'route_invalid',
        'switch_failed',
        'switching',
        'unreachable',
        'unsupported_build'
      ].sort()
    )
  })

  it('allows send when Sessions is idle', () => {
    expect(evaluateWorkspaceSend({ ...idle, isNewChat: true, ambientTupleValid: true })).toEqual({
      allowed: true,
      state: 'idle_fleet'
    })
  })

  it('projects verdicts to stable scalar snapshots for external-store subscriptions', () => {
    expect(blockedWorkspaceSendState({ ...idle, isNewChat: true, ambientTupleValid: true })).toBeNull()
    expect(blockedWorkspaceSendState({ ...idle, gatewaySwitching: true })).toBe('switching')
    expect(blockedWorkspaceSendState({ ...idle, unreachable: true })).toBe('unreachable')
  })

  it('fails closed for a new chat when the ambient tuple is invalid', () => {
    expect(
      evaluateWorkspaceSend({
        ...idle,
        ambientTupleValid: false,
        isNewChat: true
      })
    ).toEqual({ allowed: false, state: 'route_invalid' })
  })

  it('fails closed unless an allow-state is explicit', () => {
    expect(evaluateWorkspaceSend(idle)).toEqual({ allowed: false, state: 'route_invalid' })
    expect(isWorkspaceSendBlocked(idle)).toBe(true)
  })

  it('blocks while phase-1 dial is pending', () => {
    expect(
      evaluateWorkspaceSend({
        gatewaySwitching: false,
        pendingConnectionId: 'pop-os-hermes'
      })
    ).toEqual({ allowed: false, state: 'switching' })
    expect(
      isWorkspaceSendBlocked({
        gatewaySwitching: false,
        pendingConnectionId: 'pop-os-hermes'
      })
    ).toBe(true)
  })

  it('blocks while phase-2 commit is in flight', () => {
    expect(evaluateWorkspaceSend({ gatewaySwitching: true, pendingConnectionId: null })).toEqual({
      allowed: false,
      state: 'switching'
    })
  })

  it('blocks a submit that captured an older switch generation', () => {
    expect(
      evaluateWorkspaceSend({
        ...idle,
        capturedGeneration: 3,
        currentGeneration: 4
      })
    ).toEqual({ allowed: false, state: 'switching' })
  })

  it('allows send when the captured generation is still current', () => {
    expect(
      evaluateWorkspaceSend({
        ...idle,
        capturedGeneration: 7,
        currentGeneration: 7,
        isNewChat: true,
        ambientTupleValid: true
      })
    ).toEqual({ allowed: true, state: 'idle_fleet' })
  })

  it('does not treat Bot Mode talk-across as a Sessions switch', () => {
    expect(
      evaluateWorkspaceSend({
        ...idle,
        botTalkAcross: true,
        intendedOwner: { connectionId: 'mac-mini', profile: 'ops' },
        socketOwner: { connectionId: 'local', profile: 'default' }
      })
    ).toEqual({ allowed: true, state: 'bot_talk_across' })
  })

  it('still blocks Bot Mode talk-across while a real global Sessions switch is in flight', () => {
    expect(
      evaluateWorkspaceSend({
        botTalkAcross: true,
        gatewaySwitching: true,
        intendedOwner: { connectionId: 'mac-mini', profile: 'ops' },
        pendingConnectionId: null,
        socketOwner: { connectionId: 'local', profile: 'default' }
      })
    ).toEqual({ allowed: false, state: 'switching' })
  })

  it('allows a fresh new chat when the ambient current tuple is valid', () => {
    expect(
      evaluateWorkspaceSend({
        ...idle,
        ambientTupleValid: true,
        isNewChat: true
      })
    ).toEqual({ allowed: true, state: 'idle_fleet' })
  })

  it('marks an owned session already on its socket as already_active', () => {
    expect(
      evaluateWorkspaceSend({
        ...idle,
        intendedOwner: { connectionId: 'local', profile: 'mac-cockpit' },
        socketOwner: { connectionId: 'local', profile: 'mac-cockpit' }
      })
    ).toEqual({ allowed: true, state: 'already_active' })
  })

  it('fails closed for a stored session when the route is missing', () => {
    expect(
      evaluateWorkspaceSend({
        ...idle,
        isNewChat: false,
        intendedOwner: null
      })
    ).toEqual({ allowed: false, state: 'route_invalid' })
  })

  it('fails closed when intended tuple and socket owner differ (non-bot)', () => {
    expect(
      evaluateWorkspaceSend({
        ...idle,
        intendedOwner: { connectionId: 'pop-os-hermes', profile: 'default' },
        socketOwner: { connectionId: 'local', profile: 'mac-cockpit' }
      })
    ).toEqual({ allowed: false, state: 'route_invalid' })
  })

  it('fails closed when auth or runtime readiness is unresolved', () => {
    expect(
      evaluateWorkspaceSend({
        ...idle,
        isNewChat: true,
        ambientTupleValid: true,
        readinessResolved: false
      })
    ).toEqual({ allowed: false, state: 'auth_required' })
  })

  it('fails closed for auth_required, unreachable, unsupported_build, and switch_failed', () => {
    expect(evaluateWorkspaceSend({ ...idle, authRequired: true }).state).toBe('auth_required')
    expect(evaluateWorkspaceSend({ ...idle, unreachable: true }).state).toBe('unreachable')
    expect(evaluateWorkspaceSend({ ...idle, unsupportedBuild: true }).state).toBe('unsupported_build')
    expect(evaluateWorkspaceSend({ ...idle, switchFailed: true }).state).toBe('switch_failed')
    expect(evaluateWorkspaceSend({ ...idle, authRequired: true }).allowed).toBe(false)
  })

  it('treats every typed workspace rejection as a queue deferral, not a failed turn', () => {
    for (const reason of [
      'switching',
      'route_invalid',
      'auth_required',
      'unreachable',
      'unsupported_build',
      'switch_failed'
    ] satisfies WorkspaceSendSurfaceState[]) {
      const deferred = { ok: false as const, reason }

      expect(isSubmitDeferred(deferred)).toBe(true)
      expect(isSubmitAccepted(deferred)).toBe(false)
    }

    expect(isSubmitAccepted(false)).toBe(false)
    expect(isSubmitAccepted(true)).toBe(true)
    expect(isSubmitAccepted({ ok: true })).toBe(true)
    expect(isSubmitDeferred(false)).toBe(false)
  })
})

describe('collectWorkspaceSendInput', () => {
  afterEach(() => {
    $pendingConnectionId.set(null)
    endGatewaySwitch()
    $gatewaySwitching.set(false)
    $connection.set(null)
    $gatewayState.set('idle')
    $connectionsRegistry.set(null)
    clearAllSessionStates()
  })

  it('fails closed for a new chat when registry topology leaves the ambient tuple invalid', () => {
    $connectionsRegistry.set({ connections: [] } as never)
    $connection.set(null)

    const input = collectWorkspaceSendInput({})

    expect(input.isNewChat).toBe(true)
    expect(input.ambientTupleValid).toBe(false)
    expect(evaluateWorkspaceSend(input)).toEqual({ allowed: false, state: 'route_invalid' })
  })

  it('fails closed for a stored session with missing owner once registry topology exists', () => {
    $connectionsRegistry.set({ connections: [] } as never)
    $connection.set({ connectionId: 'local', profile: 'default' } as never)

    const input = collectWorkspaceSendInput({ sessionId: 'rt-unknown', storedSessionId: 'stored-unknown' })

    expect(input.isNewChat).toBe(false)
    expect(input.intendedOwner).toBeNull()
    expect(evaluateWorkspaceSend(input)).toEqual({ allowed: false, state: 'route_invalid' })
  })

  it('fails closed when runtime readiness is unresolved', () => {
    $gatewayState.set('connecting')

    const input = collectWorkspaceSendInput({})

    expect(input.readinessResolved).toBe(false)
    expect(evaluateWorkspaceSend({ ...input, isNewChat: true, ambientTupleValid: true })).toEqual({
      allowed: false,
      state: 'auth_required'
    })
  })
})
