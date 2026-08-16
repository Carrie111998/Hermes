import type { Contribution } from '@hermes/plugin-sdk'
import { describe, expect, it, vi } from 'vitest'

import { resolveKanbanTaskActions } from './task-actions'

const action = (overrides: Record<string, unknown> = {}): Contribution => ({
  area: 'kanban.task.actions',
  data: { label: 'Discuss task', run: vi.fn(), ...overrides },
  id: 'plugin:test:discuss'
})

describe('resolveKanbanTaskActions', () => {
  it('keeps valid actions in contribution order', () => {
    const resolved = resolveKanbanTaskActions([action(), action({ label: ' Second ' })], 'card')

    expect(resolved.map(item => item.label)).toEqual(['Discuss task', 'Second'])
  })

  it('filters actions that do not target the requested location', () => {
    const resolved = resolveKanbanTaskActions([action({ locations: ['drawer-menu'] })], 'card')

    expect(resolved).toEqual([])
  })

  it('drops malformed payloads instead of breaking the Kanban host', () => {
    const malformed = [
      { area: 'kanban.task.actions', data: null, id: 'null' },
      action({ label: '' }),
      action({ run: 'not-a-function' })
    ] satisfies Contribution[]

    expect(resolveKanbanTaskActions(malformed, 'card')).toEqual([])
  })
})