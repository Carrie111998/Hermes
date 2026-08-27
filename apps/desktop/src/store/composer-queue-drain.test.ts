import { afterEach, describe, expect, it } from 'vitest'

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
  afterEach(() => _resetComposerQueueDrainsForTests())

  it('excludes visible/background contenders by both qualified scope and entry', () => {
    const drain = beginComposerQueueDrain(scopeA, 'entry-a')

    expect(drain).not.toBeNull()
    expect(beginComposerQueueDrain(scopeA, 'entry-b')).toBeNull()
    expect(beginComposerQueueDrain(scopeB, 'entry-a')).toBeNull()
    expect(isComposerQueueDrainExcluded(scopeA, 'other-entry')).toBe(true)
    expect(isComposerQueueDrainExcluded(scopeB, 'entry-a')).toBe(true)

    expect(finishComposerQueueDrain(drain!)).toBe(scopeA)
    expect(isComposerQueueDrainExcluded(scopeA, 'entry-a')).toBe(false)
  })

  it('hands an in-flight lock to a migrated qualified scope and finishes with the settled key', () => {
    const drain = beginComposerQueueDrain(scopeA, 'entry-a')!

    expect(handoffComposerQueueDrains(scopeA, scopeB)).toBe(1)
    expect(beginComposerQueueDrain(scopeB, 'entry-b')).toBeNull()
    expect(finishComposerQueueDrain(drain)).toBe(scopeB)
  })

  it('rejects non-canonical scopes and a handoff into another active drain', () => {
    expect(beginComposerQueueDrain('default\0stored-a', 'entry-a')).toBeNull()

    const first = beginComposerQueueDrain(scopeA, 'entry-a')!
    const second = beginComposerQueueDrain(scopeB, 'entry-b')!

    expect(handoffComposerQueueDrains(scopeA, scopeB)).toBe(0)
    expect(finishComposerQueueDrain(first)).toBe(scopeA)
    expect(finishComposerQueueDrain(second)).toBe(scopeB)
  })
})
