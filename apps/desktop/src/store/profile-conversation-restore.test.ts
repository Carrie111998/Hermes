import { beforeEach, describe, expect, it } from 'vitest'

import {
  $appliedFreshDraftProvenance,
  $profileConversationRestore,
  _resetProfileConversationRestoreForTests,
  applyFreshDraftProvenance,
  beginProfileConversationRestore,
  cancelProfileConversationRestore,
  clearAppliedFreshDraftProvenance,
  commitProfileConversationRestore,
  createFreshSessionIntent,
  completeProfileConversationRestore,
  isCurrentProfileConversationRestore,
  provenanceForFreshSessionIntent,
  markProfileConversationRestoreNavigating,
  promoteAutomaticFreshDraftToExplicit
} from './profile-conversation-restore'

describe('profile conversation restore coordinator', () => {
  beforeEach(_resetProfileConversationRestoreForTests)

  it('normalizes targets and supersedes older generations', () => {
    const first = beginProfileConversationRestore('profile-switch', { connectionId: ' remote-a ', profile: ' work ' })
    const second = beginProfileConversationRestore('connection-switch', { connectionId: ' ', profile: ' ' })

    expect([first, second]).toEqual([1, 2])
    expect($profileConversationRestore.get()).toEqual({
      origin: 'connection-switch',
      phase: 'activating',
      sequence: 2,
      target: { connectionId: null, profile: 'default' }
    })
    expect(isCurrentProfileConversationRestore(first)).toBe(false)
    expect(isCurrentProfileConversationRestore(second)).toBe(true)
  })

  it('makes stale commit, cancel, completion, and navigation no-ops', () => {
    const stale = beginProfileConversationRestore('profile-switch', { connectionId: null, profile: 'work' })
    const current = beginProfileConversationRestore('profile-switch', { connectionId: null, profile: 'personal' })

    expect(commitProfileConversationRestore(stale)).toBe(false)
    cancelProfileConversationRestore(stale, 'activation-failed')
    completeProfileConversationRestore(stale)
    expect(markProfileConversationRestoreNavigating(stale, 'stale-session')).toBe(false)
    expect($profileConversationRestore.get()?.sequence).toBe(current)
  })

  it('commits and claims navigation only through valid phases', () => {
    const sequence = beginProfileConversationRestore('profile-switch', { connectionId: null, profile: 'work' })

    expect(markProfileConversationRestoreNavigating(sequence, 'session-1')).toBe(false)
    expect(commitProfileConversationRestore(sequence)).toBe(true)
    expect(commitProfileConversationRestore(sequence)).toBe(false)
    expect(markProfileConversationRestoreNavigating(sequence, ' ')).toBe(false)
    expect(markProfileConversationRestoreNavigating(sequence, ' session-1 ')).toBe(true)
    expect($profileConversationRestore.get()).toMatchObject({ phase: 'navigating', sequence, sessionId: 'session-1' })

    completeProfileConversationRestore(sequence)
    expect($profileConversationRestore.get()).toBeNull()
  })

  it('cancels a request without converting its automatic draft provenance', () => {
    const sequence = beginProfileConversationRestore('profile-switch', { connectionId: null, profile: 'work' })
    const provenance = {
      cause: 'profile-switch',
      freshSequence: 4,
      kind: 'automatic',
      restoreSequence: sequence
    } as const

    applyFreshDraftProvenance(provenance)
    cancelProfileConversationRestore(undefined, 'explicit-navigation')

    expect($profileConversationRestore.get()).toBeNull()
    expect($appliedFreshDraftProvenance.get()).toEqual(provenance)
  })

  it('creates globally monotonic typed fresh intents and converts them to applied provenance', () => {
    const automatic = createFreshSessionIntent({
      cause: 'profile-switch',
      persistence: 'automatic',
      restoreSequence: 7
    })
    const explicit = createFreshSessionIntent({ cause: 'new-chat', persistence: 'explicit' })

    expect(automatic.sequence).toBe(1)
    expect(explicit.sequence).toBe(2)
    expect(provenanceForFreshSessionIntent(automatic)).toEqual({
      cause: 'profile-switch',
      freshSequence: 1,
      kind: 'automatic',
      restoreSequence: 7
    })
    expect(provenanceForFreshSessionIntent(explicit)).toEqual({
      cause: 'new-chat',
      freshSequence: 2,
      kind: 'explicit'
    })
  })

  it('tracks automatic and explicit applied provenance independently', () => {
    applyFreshDraftProvenance({
      cause: 'connection-switch',
      freshSequence: 2,
      kind: 'automatic',
      restoreSequence: 1
    })
    expect($appliedFreshDraftProvenance.get()?.kind).toBe('automatic')

    applyFreshDraftProvenance({ cause: 'new-chat', freshSequence: 3, kind: 'explicit' })
    expect($appliedFreshDraftProvenance.get()).toEqual({ cause: 'new-chat', freshSequence: 3, kind: 'explicit' })

    clearAppliedFreshDraftProvenance()
    expect($appliedFreshDraftProvenance.get()).toBeNull()
  })

  it('promotes only an automatic draft and cancels its pending restore synchronously', () => {
    const sequence = beginProfileConversationRestore('profile-switch', { connectionId: null, profile: 'work' })
    applyFreshDraftProvenance({
      cause: 'profile-switch',
      freshSequence: 1,
      kind: 'automatic',
      restoreSequence: sequence
    })

    promoteAutomaticFreshDraftToExplicit()

    expect($profileConversationRestore.get()).toBeNull()
    expect($appliedFreshDraftProvenance.get()).toEqual({
      cause: 'message-on-automatic-draft',
      freshSequence: 1,
      kind: 'explicit'
    })
  })

  it('resets atoms and generation state for tests', () => {
    beginProfileConversationRestore('profile-switch', { connectionId: null, profile: 'work' })
    applyFreshDraftProvenance({ cause: 'new-chat', freshSequence: 9, kind: 'explicit' })

    _resetProfileConversationRestoreForTests()

    expect($profileConversationRestore.get()).toBeNull()
    expect($appliedFreshDraftProvenance.get()).toBeNull()
    expect(beginProfileConversationRestore('profile-switch', { connectionId: null, profile: 'work' })).toBe(1)
  })
})
