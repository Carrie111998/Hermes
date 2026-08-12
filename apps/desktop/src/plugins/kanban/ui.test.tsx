import { describe, expect, it } from 'vitest'

import type { KanbanTask } from './types'
import { arcState, isLockedTarget, LOCKED_COLUMNS } from './ui'


describe('awaiting-human desktop semantics', () => {
  it('treats the human gate as a locked system column', () => {
    expect(LOCKED_COLUMNS).toContain('awaiting_human')
    expect(isLockedTarget('awaiting_human')).toBe(true)
  })

  it('never presents an awaiting-human card as queued worker activity', () => {
    const task = {
      assignee: 'srdja',
      status: 'awaiting_human'
    } as KanbanTask

    expect(arcState(task, 'default')).toBeNull()
  })
})
