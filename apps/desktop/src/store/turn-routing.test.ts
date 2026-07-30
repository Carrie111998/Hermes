import { beforeEach, describe, expect, it } from 'vitest'

import {
  $routingBudget,
  $routingCapability,
  $turnRoutes,
  acceptRoutingBudget,
  acceptRoutingCapability,
  beginRoutingBudgetRequest,
  beginRoutingCapabilityRequest,
  beginRoutingModeWrite,
  clearTurnRoutes,
  ingestTurnRouteEvent,
  rejectRoutingBudget,
  rejectRoutingCapability
} from './turn-routing'

beforeEach(() => {
  clearTurnRoutes()
  $routingBudget.set({ available: false, error: null, generation: 0, loading: false, profile: '' })
  $routingCapability.set({
    available: false,
    error: null,
    loading: false,
    mode: 'off',
    profile: '',
    version: 0
  })
})

describe('turn routing backend truth', () => {
  it('isolates route state by session and rejects delayed events from an old turn', () => {
    expect(
      ingestTurnRouteEvent('session-a', 'route.decided', {
        mode: 'observe',
        reason_code: 'architecture_complexity',
        route: 'deep-moa',
        should_apply: false,
        source: 'rule',
        target: { kind: 'moa', preset: 'deep' },
        turn_id: 'turn-1'
      })
    ).toBe(true)
    expect(
      ingestTurnRouteEvent('session-a', 'route.decided', {
        mode: 'observe',
        reason_code: 'plain_default',
        route: 'k3',
        should_apply: false,
        source: 'rule',
        target: { kind: 'model', model: 'k3', provider: 'kimi-coding' },
        turn_id: 'turn-2'
      })
    ).toBe(true)

    expect(ingestTurnRouteEvent('session-a', 'route.completed', { turn_id: 'turn-1' })).toBe(false)
    expect($turnRoutes.get()['session-a']?.turnId).toBe('turn-2')
    expect($turnRoutes.get()['session-a']?.route).toBe('k3')
    expect($turnRoutes.get()['session-b']).toBeUndefined()
  })

  it('distinguishes unavailable capability from a temporary refresh error', () => {
    const initial = beginRoutingCapabilityRequest('default')
    expect(acceptRoutingCapability(initial, 'default', 'observe', 1)).toBe(true)

    const refresh = beginRoutingCapabilityRequest('default')
    expect(rejectRoutingCapability(refresh, 'default', new Error('timeout'))).toBe(true)
    expect($routingCapability.get()).toMatchObject({ available: true, error: 'timeout', mode: 'observe' })

    const incompatible = beginRoutingCapabilityRequest('default')
    expect(rejectRoutingCapability(incompatible, 'default', 'unknown config key', true)).toBe(true)
    expect($routingCapability.get()).toMatchObject({ available: false, error: 'unknown config key' })
  })

  it('rejects stale async capability results after a profile switch', () => {
    const stale = beginRoutingCapabilityRequest('default')
    const current = beginRoutingCapabilityRequest('work')

    expect(acceptRoutingCapability(stale, 'default', 'auto', 1)).toBe(false)
    expect(acceptRoutingCapability(current, 'work', 'off', 1)).toBe(true)
    expect($routingCapability.get()).toMatchObject({ mode: 'off', profile: 'work' })
  })

  it('does not relabel stale authority when a new profile refresh fails', () => {
    const oldCapability = beginRoutingCapabilityRequest('default')
    expect(acceptRoutingCapability(oldCapability, 'default', 'observe', 1)).toBe(true)
    const oldBudget = beginRoutingBudgetRequest('default')
    expect(
      acceptRoutingBudget(oldBudget, 'default', {
        available_slots: 1,
        committed_slots: 1,
        reserved_slots: 0,
        week_key: '2026-07-27',
        weekly_limit: 2
      })
    ).toBe(true)

    const capability = beginRoutingCapabilityRequest('work')
    const budget = beginRoutingBudgetRequest('work')

    expect($routingCapability.get()).toMatchObject({ available: false, mode: 'off', profile: 'work' })
    expect($routingBudget.get()).toMatchObject({ available: false, profile: 'work' })
    expect($routingBudget.get().status).toBeUndefined()

    expect(rejectRoutingCapability(capability, 'work', new Error('offline'))).toBe(true)
    expect(rejectRoutingBudget(budget, 'work', new Error('offline'))).toBe(true)
    expect($routingCapability.get()).toMatchObject({ available: false, mode: 'off', profile: 'work' })
    expect($routingBudget.get()).toMatchObject({ available: false, profile: 'work' })
    expect($routingBudget.get().status).toBeUndefined()
  })

  it('rejects delayed turn-start events using backend turn sequence', () => {
    expect(
      ingestTurnRouteEvent('session-a', 'route.decided', {
        mode: 'observe',
        route: 'current',
        turn_id: 'turn-new',
        turn_sequence: 2
      })
    ).toBe(true)
    expect(
      ingestTurnRouteEvent('session-a', 'route.decided', {
        mode: 'observe',
        route: 'stale',
        turn_id: 'turn-old',
        turn_sequence: 1
      })
    ).toBe(false)
    expect($turnRoutes.get()['session-a']).toMatchObject({ route: 'current', turnId: 'turn-new', turnSequence: 2 })
  })

  it('preserves selection reason fidelity across terminal events', () => {
    expect(
      ingestTurnRouteEvent('session-a', 'route.decided', {
        mode: 'observe',
        reason_code: 'architecture_complexity',
        route: 'deep',
        turn_id: 'turn-1',
        turn_sequence: 1
      })
    ).toBe(true)
    expect(
      ingestTurnRouteEvent('session-a', 'route.completed', {
        mode: 'observe',
        reason_code: 'route_completed',
        route: 'deep',
        selection_reason_code: 'architecture_complexity',
        turn_id: 'turn-1',
        turn_sequence: 1
      })
    ).toBe(true)
    expect($turnRoutes.get()['session-a']).toMatchObject({
      event: 'route.completed',
      reasonCode: 'architecture_complexity'
    })
  })

  it('rolls back a failed optimistic write without letting an old failure clobber newer intent', () => {
    const seed = beginRoutingCapabilityRequest('default')
    acceptRoutingCapability(seed, 'default', 'off', 1)
    const old = beginRoutingModeWrite('default', 'observe')
    const current = beginRoutingModeWrite('default', 'auto')

    expect(rejectRoutingCapability(old.generation, 'default', 'old failure', false, old.previous)).toBe(false)
    expect($routingCapability.get().mode).toBe('auto')
    expect(rejectRoutingCapability(current.generation, 'default', 'denied', false, current.previous)).toBe(true)
    expect($routingCapability.get()).toMatchObject({ error: 'denied', mode: 'observe' })
  })

  it('keeps budget status profile-scoped and rejects a stale response', () => {
    const oldGeneration = beginRoutingBudgetRequest('default')
    const currentGeneration = beginRoutingBudgetRequest('work')

    expect(
      acceptRoutingBudget(oldGeneration, 'default', {
        available_slots: 1,
        committed_slots: 1,
        cooldown_reason_code: null,
        cooldown_until_at: null,
        reserved_slots: 0,
        scope: 'grok',
        week_key: '2026-07-27',
        weekly_limit: 2
      })
    ).toBe(false)
    expect(
      acceptRoutingBudget(currentGeneration, 'work', {
        available_slots: 0,
        committed_slots: 0,
        cooldown_reason_code: 'provider_rate_limited',
        cooldown_until_at: 1_800_000_000,
        reserved_slots: 0,
        scope: 'grok',
        week_key: '2026-07-27',
        weekly_limit: 0
      })
    ).toBe(true)
    expect($routingBudget.get()).toMatchObject({
      available: true,
      profile: 'work',
      status: { cooldownReasonCode: 'provider_rate_limited', weeklyLimit: 0 }
    })
  })
})
