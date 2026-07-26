import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { derivePetState } from './pet'
import {
  __resetPetMultiForTests,
  completeTool,
  deriveProfileActivity,
  getProfilePet,
  markProfilePetUnread,
  clearProfilePetUnread,
  removeSessionActivity,
  replaceSessionRuntimeId,
  setProfileSourceDurableSessionId,
  setSessionAwaitingInput,
  setSessionBusy,
  setSessionReasoning,
  startTool,
  terminateSession
} from './pet-multi'

beforeEach(() => {
  __resetPetMultiForTests()
})

afterEach(() => {
  vi.useRealTimers()
  __resetPetMultiForTests()
})

describe('session-derived activity', () => {
  it('two concurrent sessions in one profile keep toolRunning until BOTH finish (test 5)', () => {
    startTool('default', 'session-a', 'tool-1')
    startTool('default', 'session-b', 'tool-2')

    expect(deriveProfileActivity('default').toolRunning).toBe(true)

    // Finishing session A's tool leaves session B still running.
    completeTool('default', 'session-a', 'tool-1')
    expect(deriveProfileActivity('default').toolRunning).toBe(true)

    completeTool('default', 'session-b', 'tool-2')
    expect(deriveProfileActivity('default').toolRunning).toBe(false)
  })

  it('overlapping tool ids in ONE session keep toolRunning true until the last completes (test 21)', () => {
    startTool('default', 'sess', 'a')
    startTool('default', 'sess', 'b')
    expect(deriveProfileActivity('default').toolRunning).toBe(true)

    completeTool('default', 'sess', 'a')
    expect(deriveProfileActivity('default').toolRunning).toBe(true) // b still running

    completeTool('default', 'sess', 'b')
    expect(deriveProfileActivity('default').toolRunning).toBe(false)
  })

  it('ignores empty/missing tool ids (no phantom tools)', () => {
    startTool('default', 'sess', '')
    startTool('default', 'sess', undefined)
    expect(deriveProfileActivity('default').toolRunning).toBe(false)
  })

  it('aggregates busy/reasoning across sessions with the profile prefix only', () => {
    setSessionBusy('default', 'sess-1', true)
    setSessionReasoning('apollo', 'sess-2', true)

    expect(deriveProfileActivity('default').busy).toBe(true)
    expect(deriveProfileActivity('default').reasoning).toBe(false)
    expect(deriveProfileActivity('apollo').reasoning).toBe(true)
    expect(deriveProfileActivity('apollo').busy).toBe(false)
  })
})

describe('terminal transitions', () => {
  it('session.info running=false clears busy/reasoning/tools (test 40)', () => {
    setSessionBusy('default', 'sess', true)
    setSessionReasoning('default', 'sess', true)
    startTool('default', 'sess', 'tool')
    expect(deriveProfileActivity('default').busy).toBe(true)

    terminateSession('default', 'sess')

    const activity = deriveProfileActivity('default')
    expect(activity.busy).toBe(false)
    expect(activity.reasoning).toBe(false)
    expect(activity.toolRunning).toBe(false)
  })

  it('interrupt/error/removal clean up the activity entry (test 20)', () => {
    setSessionBusy('default', 'sess', true)
    startTool('default', 'sess', 'tool')

    // Error terminal clears steady state and fires an error beat.
    terminateSession('default', 'sess', { error: true })
    expect(deriveProfileActivity('default').busy).toBe(false)
    expect(deriveProfileActivity('default').error).toBe(true)

    // Removal drops the entry entirely.
    setSessionBusy('default', 'sess2', true)
    removeSessionActivity('default', 'sess2')
    expect(deriveProfileActivity('default').busy).toBe(false)
  })

  it('normal completion celebrates then settles to idle — NOT waiting (test 42)', () => {
    vi.useFakeTimers()
    setSessionBusy('default', 'sess', true)
    setSessionAwaitingInput('default', 'sess', true)

    terminateSession('default', 'sess', { celebrate: true })

    const activity = deriveProfileActivity('default')
    expect(activity.celebrate).toBe(true)
    expect(activity.busy).toBe(false)
    // A normal completion clears awaitingInput — it returns to idle, not waiting.
    expect(activity.awaitingInput).toBe(false)
    expect(derivePetState(activity)).toBe('jump')

    // After the beat decays, the pet settles to idle (not waiting).
    vi.advanceTimersByTime(2300)
    const settled = deriveProfileActivity('default')
    expect(settled.celebrate).toBe(false)
    expect(derivePetState(settled)).toBe('idle')
  })

  it('clarify/approval sets awaitingInput; resolution clears it (test 43)', () => {
    setSessionAwaitingInput('default', 'sess', true)
    expect(deriveProfileActivity('default').awaitingInput).toBe(true)
    expect(derivePetState(deriveProfileActivity('default'))).toBe('waiting')

    setSessionAwaitingInput('default', 'sess', false)
    expect(deriveProfileActivity('default').awaitingInput).toBe(false)
  })

  it('runtime-id replacement migrates the activity entry', () => {
    setSessionBusy('default', 'old-runtime', true)
    replaceSessionRuntimeId('default', 'old-runtime', 'new-runtime')

    expect(deriveProfileActivity('default').busy).toBe(true)
    // The old key no longer holds state; terminating the new id clears it.
    terminateSession('default', 'new-runtime')
    expect(deriveProfileActivity('default').busy).toBe(false)
  })
})

describe('per-profile unread (source-session scoped)', () => {
  it('clearing unread for one source session does not erase another in the same profile (test 46 prep)', () => {
    markProfilePetUnread('default', 'session-a')
    expect(getProfilePet('default')?.unread).toBe(true)

    // Focusing session B must NOT clear session A's unread.
    clearProfilePetUnread('default', 'session-b')
    expect(getProfilePet('default')?.unread).toBe(true)

    // Focusing session A clears it.
    clearProfilePetUnread('default', 'session-a')
    expect(getProfilePet('default')?.unread).toBe(false)
  })
})

describe('sourceDurableSessionId (overlay openApp)', () => {
  it('records the durable id on an existing slice and is a no-op before one exists', () => {
    // No slice yet → no-op (doesn't conjure an empty profile into existence).
    setProfileSourceDurableSessionId('apollo', 'stored-1')
    expect(getProfilePet('apollo')).toBeUndefined()

    // Once the profile has a slice (e.g. unread marked), the durable id sticks.
    markProfilePetUnread('apollo', 'rt-1')
    setProfileSourceDurableSessionId('apollo', 'stored-1')
    expect(getProfilePet('apollo')?.sourceDurableSessionId).toBe('stored-1')
  })
})
