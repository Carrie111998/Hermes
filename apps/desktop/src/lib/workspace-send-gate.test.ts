import { describe, expect, it } from 'vitest'

import {
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
    expect(isWorkspaceSendBlocked(idle)).toBe(false)
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
    expect(isWorkspaceSendBlocked(idle)).toBe(false)
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

  it('treats a switching submit result as a deferral, not a failure', () => {
    const deferred = { ok: false as const, reason: 'switching' as WorkspaceSendSurfaceState }

    expect(isSubmitDeferred(deferred)).toBe(true)
    expect(isSubmitAccepted(deferred)).toBe(false)
    expect(isSubmitAccepted(false)).toBe(false)
    expect(isSubmitAccepted(true)).toBe(true)
    expect(isSubmitDeferred(false)).toBe(false)
  })
})
