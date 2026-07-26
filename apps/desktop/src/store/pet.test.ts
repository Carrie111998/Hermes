import { describe, expect, it } from 'vitest'

import {
  $petActivity,
  $petAtRest,
  $petMotion,
  $petReplyText,
  $petState,
  clearPetReplyText,
  derivePetState,
  flashPetActivity,
  replacePetActivity,
  setPetActivity,
  setPetReplyText,
  shouldMarkUnread
} from './pet'

describe('derivePetState', () => {
  it('rests at idle by default and uses waiting when awaiting input', () => {
    expect(derivePetState({})).toBe('idle')
    expect(derivePetState({ awaitingInput: true })).toBe('waiting')
  })

  it('runs when busy or a tool is executing', () => {
    expect(derivePetState({ busy: true })).toBe('run')
    expect(derivePetState({ toolRunning: true })).toBe('run')
  })

  it('reviews while reasoning (below tool, above bare busy)', () => {
    expect(derivePetState({ reasoning: true })).toBe('review')
    expect(derivePetState({ reasoning: true, busy: true })).toBe('review')
    expect(derivePetState({ reasoning: true, toolRunning: true })).toBe('run')
  })

  it('waits (blocked on the user) above the in-flight signals', () => {
    expect(derivePetState({ awaitingInput: true, toolRunning: true, busy: true })).toBe('waiting')
    // but a finish beat still wins over waiting
    expect(derivePetState({ justCompleted: true, awaitingInput: true })).toBe('wave')
  })

  it('honors the full priority chain: error > celebrate > complete > tool', () => {
    expect(derivePetState({ error: true, celebrate: true, busy: true })).toBe('failed')
    expect(derivePetState({ celebrate: true, justCompleted: true, toolRunning: true })).toBe('jump')
    expect(derivePetState({ justCompleted: true, toolRunning: true })).toBe('wave')
  })
})

describe('roam motion', () => {
  it('only reports at-rest when the agent-driven state is plain idle', () => {
    replacePetActivity({})
    expect($petAtRest.get()).toBe(true)

    replacePetActivity({ busy: true })
    expect($petAtRest.get()).toBe(false)

    replacePetActivity({})
    expect($petAtRest.get()).toBe(true)
  })

  it('shows the roam pose while wandering, but never overrides real activity', () => {
    replacePetActivity({})
    $petMotion.set('run')
    expect($petState.get()).toBe('run')

    // Hops surface the jump pose.
    $petMotion.set('jump')
    expect($petState.get()).toBe('jump')

    // Activity wins over a wander in progress.
    replacePetActivity({ reasoning: true, busy: true })
    expect($petState.get()).toBe('review')

    // Back at rest, the wander resumes its pose; clearing it returns to idle.
    replacePetActivity({})
    expect($petState.get()).toBe('jump')
    $petMotion.set(null)
    expect($petState.get()).toBe('idle')

    replacePetActivity({})
  })
})

describe('flashPetActivity', () => {
  it('clears stale sibling beats so a completion never inherits a prior error', () => {
    // A turn errors (sad), then the next turn finishes cleanly. The celebrate
    // beat must win — error is highest priority, so a merge-only flash would
    // keep the pet on the failed pose.
    setPetActivity({ error: true })
    flashPetActivity({ celebrate: true })

    expect($petActivity.get().error).toBe(false)
    expect($petState.get()).toBe('jump')

    setPetActivity({})
  })
})

describe('replyText', () => {
  it('stores trimmed reply text', () => {
    setPetReplyText('  hello world  ')
    expect($petReplyText.get()).toBe('hello world')
  })

  it('skips empty / whitespace-only text', () => {
    clearPetReplyText()
    setPetReplyText('   ')
    expect($petReplyText.get()).toBeNull()
  })

  it('skips [SILENT] replies', () => {
    clearPetReplyText()
    setPetReplyText('[SILENT] heartbeat sent')
    expect($petReplyText.get()).toBeNull()
  })

  it('truncates to 200 chars', () => {
    const long = 'a'.repeat(250)
    setPetReplyText(long)
    expect($petReplyText.get()?.length).toBe(200)
  })
})

describe('shouldMarkUnread (pure)', () => {
  const base = {
    activeProfile: 'default',
    activeSessionId: 's1',
    profile: 'default',
    sessionId: 's1',
    windowFocused: true
  }

  it('does not mark the active session unread while the window is focused', () => {
    expect(shouldMarkUnread(base)).toBe(false)
  })

  it('marks unread when the window is unfocused', () => {
    expect(shouldMarkUnread({ ...base, windowFocused: false })).toBe(true)
  })

  it('marks a background profile unread even while focused (pinned mode, test 22)', () => {
    expect(shouldMarkUnread({ ...base, profile: 'apollo', sessionId: 's2' })).toBe(true)
  })

  it('marks a non-active session in the active profile unread while focused', () => {
    expect(shouldMarkUnread({ ...base, sessionId: 's2' })).toBe(true)
  })

  it('a sessionless event (null sessionId) on the active profile does not mark unread while focused', () => {
    // The session clause requires sessionId !== null, so a global/sessionless
    // event on the active profile stays quiet while the window is focused.
    expect(shouldMarkUnread({ ...base, sessionId: null })).toBe(false)
  })
})
