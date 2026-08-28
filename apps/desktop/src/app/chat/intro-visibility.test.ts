import { describe, expect, it } from 'vitest'

import { shouldShowConversationRestoreLoading, shouldShowIntro } from './intro-visibility'

const showing = {
  activeSessionId: null,
  auxiliaryWindow: false,
  enabled: true,
  freshDraftReady: true,
  messagesEmpty: true,
  primary: true,
  restoringConversation: false,
  routedSessionView: false,
  selectedSessionId: null
} as const

describe('shouldShowConversationRestoreLoading', () => {
  const profileRestore = {
    origin: 'profile-switch',
    phase: 'activating',
    sequence: 7,
    target: { connectionId: null, profile: 'work' }
  } as const
  const connectionRestore = {
    origin: 'connection-switch',
    phase: 'activating',
    sequence: 9,
    target: { connectionId: 'homelab', profile: 'default' }
  } as const
  const matchingProvenance = {
    cause: 'connection-switch',
    freshSequence: 12,
    kind: 'automatic',
    restoreSequence: 9
  } as const

  it('masks profile restores from activation through navigation', () => {
    expect(shouldShowConversationRestoreLoading({ primary: true, provenance: null, restore: profileRestore })).toBe(
      true
    )
    expect(
      shouldShowConversationRestoreLoading({
        primary: true,
        provenance: null,
        restore: { ...profileRestore, phase: 'committed' }
      })
    ).toBe(true)
    expect(
      shouldShowConversationRestoreLoading({
        primary: true,
        provenance: null,
        restore: { ...profileRestore, phase: 'navigating', sessionId: 'work-last' }
      })
    ).toBe(true)
  })

  it('keeps the current transcript visible while a connection switch is only dialing', () => {
    expect(
      shouldShowConversationRestoreLoading({
        primary: true,
        provenance: matchingProvenance,
        restore: connectionRestore
      })
    ).toBe(false)
  })

  it('masks committed and navigating connection restores only for their matching automatic draft', () => {
    expect(
      shouldShowConversationRestoreLoading({
        primary: true,
        provenance: matchingProvenance,
        restore: { ...connectionRestore, phase: 'committed' }
      })
    ).toBe(true)
    expect(
      shouldShowConversationRestoreLoading({
        primary: true,
        provenance: matchingProvenance,
        restore: { ...connectionRestore, phase: 'navigating', sessionId: 'remote-last' }
      })
    ).toBe(true)

    expect(
      shouldShowConversationRestoreLoading({
        primary: true,
        provenance: { ...matchingProvenance, restoreSequence: 8 },
        restore: { ...connectionRestore, phase: 'committed' }
      })
    ).toBe(false)
    expect(
      shouldShowConversationRestoreLoading({
        primary: true,
        provenance: { cause: 'new-chat', freshSequence: 13, kind: 'explicit' },
        restore: { ...connectionRestore, phase: 'committed' }
      })
    ).toBe(false)
  })

  it('never masks a non-primary chat surface', () => {
    expect(
      shouldShowConversationRestoreLoading({
        primary: false,
        provenance: matchingProvenance,
        restore: { ...connectionRestore, phase: 'committed' }
      })
    ).toBe(false)
  })
})

describe('shouldShowIntro', () => {
  it('shows on a fresh draft in the primary window', () => {
    expect(shouldShowIntro(showing)).toBe(true)
  })

  it('hides when the Appearance toggle is off', () => {
    expect(shouldShowIntro({ ...showing, enabled: false })).toBe(false)
  })

  it('keeps the toggle authoritative over every other clause', () => {
    // Off means off: no window, session, or draft state re-enables the splash.
    const inputs = [
      { ...showing, auxiliaryWindow: true, enabled: false },
      { ...showing, enabled: false, freshDraftReady: false },
      { ...showing, enabled: false, primary: false },
      { ...showing, enabled: false, messagesEmpty: false }
    ]

    for (const input of inputs) {
      expect(shouldShowIntro(input)).toBe(false)
    }
  })

  it('hides on surfaces that are not an empty primary draft', () => {
    expect(shouldShowIntro({ ...showing, primary: false })).toBe(false)
    expect(shouldShowIntro({ ...showing, auxiliaryWindow: true })).toBe(false)
    expect(shouldShowIntro({ ...showing, freshDraftReady: false })).toBe(false)
    expect(shouldShowIntro({ ...showing, restoringConversation: true })).toBe(false)
    expect(shouldShowIntro({ ...showing, routedSessionView: true })).toBe(false)
    expect(shouldShowIntro({ ...showing, selectedSessionId: 'session-1' })).toBe(false)
    expect(shouldShowIntro({ ...showing, activeSessionId: 'session-1' })).toBe(false)
    expect(shouldShowIntro({ ...showing, messagesEmpty: false })).toBe(false)
  })
})
