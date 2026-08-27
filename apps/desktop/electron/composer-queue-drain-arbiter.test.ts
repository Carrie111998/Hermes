import { describe, expect, it } from 'vitest'

import { ComposerQueueDrainArbiter } from './composer-queue-drain-arbiter'

describe('ComposerQueueDrainArbiter', () => {
  it('atomically excludes claims across independent renderer clients', () => {
    const arbiter = new ComposerQueueDrainArbiter()
    const first = arbiter.begin('scope-a', 'entry-a')
    const sameIdOtherOwner = arbiter.begin('scope-b', 'entry-a')

    expect(first).not.toBeNull()
    expect(sameIdOtherOwner).not.toBeNull()
    expect(arbiter.begin('scope-a', 'entry-b')).toBeNull()
    expect(arbiter.finish(first!)).toBe('scope-a')
    expect(arbiter.finish(sameIdOtherOwner!)).toBe('scope-b')
    expect(arbiter.begin('scope-a', 'entry-b')).not.toBeNull()
  })

  it('keeps migrated source and destination claims excluded until both settle', () => {
    const arbiter = new ComposerQueueDrainArbiter()
    const source = arbiter.begin('scope-a', 'entry-a')!
    const destination = arbiter.begin('scope-b', 'entry-b')!

    expect(arbiter.handoff('scope-a', 'scope-b')).toBe(1)
    expect(arbiter.begin('scope-b', 'entry-c')).toBeNull()
    expect(arbiter.finish(destination)).toBe('scope-b')
    expect(arbiter.begin('scope-b', 'entry-c')).toBeNull()
    expect(arbiter.finish(source)).toBe('scope-b')
    expect(arbiter.begin('scope-b', 'entry-c')).not.toBeNull()
  })

  it('releases every claim owned by a renderer that exits', () => {
    const arbiter = new ComposerQueueDrainArbiter()

    expect(arbiter.begin('scope-a', 'entry-a', 11)).not.toBeNull()
    expect(arbiter.begin('scope-b', 'entry-b', 22)).not.toBeNull()

    expect(arbiter.releaseOwner(11)).toBe(1)
    expect(arbiter.begin('scope-a', 'entry-c', 22)).not.toBeNull()
    expect(arbiter.begin('scope-b', 'entry-d', 11)).toBeNull()
  })
})
