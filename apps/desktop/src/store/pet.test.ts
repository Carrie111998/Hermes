import { describe, expect, it } from 'vitest'

import {
  $petActivity,
  $petAtRest,
  $petMotion,
  $petState,
  derivePetState,
  deriveSessionPetActivity,
  flashPetActivity,
  petInfoFromCache,
  setPetActivity
} from './pet'

describe('pet info startup cache', () => {
  const info = {
    enabled: true,
    slug: 'cached-pet',
    spritesheetBase64: 'd2VicA==',
    spritesheetRevision: '1:8'
  }

  it('restores a complete snapshot for the active profile', () => {
    expect(petInfoFromCache(JSON.stringify({ info, profile: 'default' }), 'default')).toEqual(info)
  })

  it('ignores another profile or an incomplete enabled snapshot', () => {
    expect(petInfoFromCache(JSON.stringify({ info, profile: 'work' }), 'default')).toEqual({ enabled: false })
    expect(petInfoFromCache(JSON.stringify({ info: { enabled: true }, profile: 'default' }), 'default')).toEqual({
      enabled: false
    })
  })

  it('ignores malformed storage', () => {
    expect(petInfoFromCache('{broken', 'default')).toEqual({ enabled: false })
  })
})

describe('derivePetState', () => {
  it('rests at idle by default and runs while any session is working', () => {
    expect(derivePetState({})).toBe('idle')
    expect(derivePetState({ busy: true })).toBe('run')
  })

  it('uses the Codex activity priority: needs input > failed > ready > running', () => {
    expect(derivePetState({ awaitingInput: true, busy: true, error: true, ready: true })).toBe('waiting')
    expect(derivePetState({ busy: true, error: true, ready: true })).toBe('failed')
    expect(derivePetState({ busy: true, ready: true })).toBe('review')
    expect(derivePetState({ busy: true })).toBe('run')
  })

  it('keeps direct reactions below actionable activity', () => {
    expect(derivePetState({ celebrate: true })).toBe('jump')
    expect(derivePetState({ celebrate: true, error: true })).toBe('failed')
    expect(derivePetState({ awaitingInput: true, celebrate: true })).toBe('waiting')
  })
})

describe('deriveSessionPetActivity', () => {
  it('keeps running for work in a background session', () => {
    expect(
      deriveSessionPetActivity({
        attentionSessionIds: [],
        failedSessionIds: [],
        unreadFinishedSessionIds: [],
        workingSessionIds: ['tile']
      })
    ).toEqual({
      awaitingInput: false,
      busy: true,
      error: false,
      ready: false
    })
  })

  it('aggregates waiting, failed, and unread-ready across different sessions', () => {
    const activity = deriveSessionPetActivity({
      attentionSessionIds: ['waiting'],
      failedSessionIds: ['blocked'],
      unreadFinishedSessionIds: ['finished'],
      workingSessionIds: ['running']
    })

    expect(activity).toEqual({ awaitingInput: true, busy: true, error: true, ready: true })
    expect(derivePetState(activity)).toBe('waiting')
  })
})

describe('roam motion', () => {
  it('only reports at-rest when the agent-driven state is plain idle', () => {
    $petActivity.set({})
    expect($petAtRest.get()).toBe(true)

    $petActivity.set({ busy: true })
    expect($petAtRest.get()).toBe(false)

    $petActivity.set({})
    expect($petAtRest.get()).toBe(true)
  })

  it('shows the roam pose while wandering, but never overrides real activity', () => {
    $petActivity.set({})
    $petMotion.set('run')
    expect($petState.get()).toBe('run')

    // Hops surface the jump pose.
    $petMotion.set('jump')
    expect($petState.get()).toBe('jump')

    // Activity wins over a wander in progress.
    $petActivity.set({ busy: true, ready: true })
    expect($petState.get()).toBe('review')

    // Back at rest, the wander resumes its pose; clearing it returns to idle.
    $petActivity.set({})
    expect($petState.get()).toBe('jump')
    $petMotion.set(null)
    expect($petState.get()).toBe('idle')

    $petActivity.set({})
  })
})

describe('flashPetActivity', () => {
  it('shows a direct reaction without replacing steady session activity', () => {
    setPetActivity({ busy: false, error: false })
    flashPetActivity({ celebrate: true })

    expect($petState.get()).toBe('jump')
    expect($petActivity.get().busy).toBe(false)

    setPetActivity({})
  })
})
