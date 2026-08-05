import { describe, expect, it } from 'vitest'

import { $memoryChange, MemoryChangeMeta, notifyMemoryChanged } from './live-sync'

describe('notifyMemoryChanged', () => {
  it('increments tick and stores meta payload', () => {
    const before = $memoryChange.get()

    const meta: MemoryChangeMeta = { profile: "coder", target: "memory", source: "memory_tool" }
    notifyMemoryChanged(meta)

    const after = $memoryChange.get()
    expect(after.tick).toBe(before.tick + 1)
    expect(after.meta).toEqual(meta)
  })

  it('works without meta (broad refresh)', () => {
    const before = $memoryChange.get()

    notifyMemoryChanged()

    const after = $memoryChange.get()
    expect(after.tick).toBe(before.tick + 1)
    expect(after.meta).toBeUndefined()
  })

  it('preserves meta across multiple ticks', () => {
    notifyMemoryChanged({ profile: 'default', target: 'user', source: 'memory_tool' })
    const first = $memoryChange.get()

    notifyMemoryChanged({ profile: "coder", target: "memory", source: 'reset' })
    const second = $memoryChange.get()

    expect(second.tick).toBe(first.tick + 1)
    expect(second.meta).toEqual({ profile: 'coder', target: 'memory', source: 'reset' })
    // First meta is gone — only the latest is kept
    expect($memoryChange.get().meta).not.toEqual({ profile: 'default', target: 'user', source: 'memory_tool' })
  })
})
