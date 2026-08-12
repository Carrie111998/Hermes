import { describe, expect, it } from 'vitest'

import { $sessionDotStateById } from './session-dot-state'
import { $foreignLiveSessionIds } from './foreign-live'
import { $sessions } from './session'
import { $sessionStates } from './session-states'

const row = (id: string, isActive: boolean) => ({
  id,
  is_active: isActive,
  last_active: Date.now() / 1000,
  last_activity_at: Date.now() / 1000 - 10,
  message_count: 3,
  profile: 'default',
  source: 'cli'
})

describe('$foreignLiveSessionIds', () => {
  it('claims is_active rows with no runtime in this renderer', () => {
    $sessions.set([row('s-cli', true) as never])
    $sessionStates.set({})
    expect($foreignLiveSessionIds.get()).toContain('s-cli')
  })

  it('does NOT claim rows that have a runtime (event-owned)', () => {
    $sessions.set([row('s-desktop', true) as never])
    $sessionStates.set({
      'runtime-1': { storedSessionId: 's-desktop', busy: true } as never
    })
    expect($foreignLiveSessionIds.get()).not.toContain('s-desktop')
  })

  it('drops the claim when is_active goes false', () => {
    $sessions.set([row('s-cli', true) as never])
    $sessionStates.set({})
    expect($foreignLiveSessionIds.get()).toContain('s-cli')
    $sessions.set([row('s-cli', false) as never])
    expect($foreignLiveSessionIds.get()).not.toContain('s-cli')
  })

  it('does NOT claim an is_active row with stale last_activity_at (reopened/ended)', () => {
    $sessions.set([
      {
        id: 's-ended',
        is_active: true, // ended_at NULL no refresh, mas sem toque real
        last_active: Date.now() / 1000,
        last_activity_at: Date.now() / 1000 - 600, // 10 min atrás
        message_count: 3,
        profile: 'default',
        source: 'cli'
      } as never
    ])
    $sessionStates.set({})
    expect($foreignLiveSessionIds.get()).not.toContain('s-ended')
  })

  it('claims a row whose last_activity_at is fresh even when is_active is stale-adjacent', () => {
    $sessions.set([
      {
        id: 's-fresh',
        is_active: true,
        last_active: Date.now() / 1000,
        last_activity_at: Date.now() / 1000 - 30, // 30s atrás: turno ativo
        message_count: 3,
        profile: 'default',
        source: 'cron'
      } as never
    ])
    $sessionStates.set({})
    expect($foreignLiveSessionIds.get()).toContain('s-fresh')
  })

  it('paints the working dot through the dot-state pipeline', () => {
    $sessions.set([row('s-cron', true) as never])
    $sessionStates.set({})
    expect($sessionDotStateById.get()['s-cron']).toBe('working')
  })
})
