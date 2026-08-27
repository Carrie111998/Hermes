import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  _resetComposerQueueDrainsForTests,
  beginComposerQueueDrain,
  finishComposerQueueDrain,
  handoffComposerQueueDrains,
  isComposerQueueDrainExcluded
} from './composer-queue-drain'
import { encodeComposerStorageScopeKey } from './composer-storage-scope'

const owner = { connectionId: 'local', profile: 'default' }
const scopeA = encodeComposerStorageScopeKey(owner, 'stored-a')
const scopeB = encodeComposerStorageScopeKey(owner, 'stored-b')

describe('composer queue drain coordinator', () => {
  const originalDesktop = window.hermesDesktop

  afterEach(() => {
    _resetComposerQueueDrainsForTests()
    window.hermesDesktop = originalDesktop
  })

  it('delegates cross-window claims and settlement to the desktop arbiter', () => {
    const begin = vi.fn().mockReturnValue(41)
    const excluded = vi.fn().mockReturnValue(true)
    const handoff = vi.fn().mockReturnValue(1)
    const finish = vi.fn().mockReturnValue(scopeB)

    window.hermesDesktop = {
      ...originalDesktop,
      composerQueueDrain: { begin, excluded, finish, handoff }
    } as never

    const drain = beginComposerQueueDrain(scopeA, 'entry-a')!

    expect(begin).toHaveBeenCalledWith({ entryId: 'entry-a', scopeKey: scopeA })
    expect(isComposerQueueDrainExcluded(scopeB, 'entry-b')).toBe(true)
    expect(excluded).toHaveBeenCalledWith({ entryId: 'entry-b', scopeKey: scopeB })
    expect(handoffComposerQueueDrains(scopeA, scopeB)).toBe(1)
    expect(handoff).toHaveBeenCalledWith({ fromScopeKey: scopeA, toScopeKey: scopeB })
    expect(finishComposerQueueDrain(drain)).toBe(scopeB)
    expect(finish).toHaveBeenCalledWith(41)
  })

  it('excludes visible/background contenders by both qualified scope and entry', () => {
    const drain = beginComposerQueueDrain(scopeA, 'entry-a')
    const sameIdOtherOwner = beginComposerQueueDrain(scopeB, 'entry-a')

    expect(drain).not.toBeNull()
    expect(sameIdOtherOwner).not.toBeNull()
    expect(beginComposerQueueDrain(scopeA, 'entry-b')).toBeNull()
    expect(isComposerQueueDrainExcluded(scopeA, 'other-entry')).toBe(true)
    expect(isComposerQueueDrainExcluded(scopeB, 'entry-a')).toBe(true)

    expect(finishComposerQueueDrain(drain!)).toBe(scopeA)
    expect(finishComposerQueueDrain(sameIdOtherOwner!)).toBe(scopeB)
    expect(isComposerQueueDrainExcluded(scopeA, 'entry-a')).toBe(false)
  })

  it('hands an in-flight lock to a migrated qualified scope and finishes with the settled key', () => {
    const drain = beginComposerQueueDrain(scopeA, 'entry-a')!

    expect(handoffComposerQueueDrains(scopeA, scopeB)).toBe(1)
    expect(beginComposerQueueDrain(scopeB, 'entry-b')).toBeNull()
    expect(finishComposerQueueDrain(drain)).toBe(scopeB)
  })

  it('merges source and destination claims so exclusion lasts until both settle', () => {
    expect(beginComposerQueueDrain('default\0stored-a', 'entry-a')).toBeNull()

    const first = beginComposerQueueDrain(scopeA, 'entry-a')!
    const second = beginComposerQueueDrain(scopeB, 'entry-b')!

    expect(handoffComposerQueueDrains(scopeA, scopeB)).toBe(1)
    expect(beginComposerQueueDrain(scopeB, 'entry-c')).toBeNull()
    expect(finishComposerQueueDrain(second)).toBe(scopeB)
    expect(beginComposerQueueDrain(scopeB, 'entry-c')).toBeNull()
    expect(finishComposerQueueDrain(first)).toBe(scopeB)
    expect(beginComposerQueueDrain(scopeB, 'entry-c')).not.toBeNull()
  })
})
