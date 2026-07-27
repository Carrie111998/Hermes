import { describe, expect, it, vi } from 'vitest'

import {
  ACTION_REGISTRY,
  ActionRegistry,
  executeAction,
  setActionContextProvider
} from '../app/actionRegistry.js'
import type { ActionContext, TuiAction } from '../domain/actions.js'

const ctx: ActionContext = {
  busy: false,
  dashboard: false,
  dispatchSlash: vi.fn(),
  hasSession: true
}

const action = (overrides: Partial<TuiAction> = {}): TuiAction => ({
  availability: () => ({ status: 'enabled' }),
  group: 'session',
  id: 'session.switch',
  run: vi.fn(),
  title: 'Switch session',
  ...overrides
})

describe('ActionRegistry', () => {
  it('registers actions deterministically and rejects duplicate IDs', () => {
    const registry = new ActionRegistry()
    const model = action({ group: 'model-profile', id: 'model.switch', title: 'Switch model' })
    const sessions = action()

    registry.register(model)
    registry.register(sessions)

    expect(registry.list()).toEqual([sessions, model])
    expect(() => registry.register(action())).toThrow('Duplicate action id: session.switch')
  })

  it('ranks title and alias prefixes ahead of fuzzy description matches', () => {
    const registry = new ActionRegistry([
      action({ aliases: ['resume'], description: 'Browse prior conversations' }),
      action({ description: 'Resume work using another provider', id: 'model.switch', title: 'Switch model' })
    ])

    expect(registry.search('res', ctx).map(result => result.action.id)).toEqual(['session.switch', 'model.switch'])
  })

  it('keeps disabled reasons and excludes hidden actions', () => {
    const registry = new ActionRegistry([
      action({ availability: () => ({ reason: 'Start a session first', status: 'disabled' }) }),
      action({ availability: () => ({ status: 'hidden' }), id: 'session.hidden', title: 'Hidden action' })
    ])

    expect(registry.search('', ctx)).toEqual([
      expect.objectContaining({
        action: expect.objectContaining({ id: 'session.switch' }),
        availability: { reason: 'Start a session first', status: 'disabled' }
      })
    ])
  })
})

describe('default TUI actions', () => {
  it('exposes the initial safe action set through existing slash routes', () => {
    const ids = ACTION_REGISTRY.list().map(item => item.id)

    expect(ids).toEqual(
      expect.arrayContaining([
        'session.switch',
        'model.switch',
        'session.new',
        'agents.open',
        'skills.open',
        'plugins.open',
        'settings.details',
        'system.logs',
        'system.quit'
      ])
    )
  })

  it('executes through the current action context and preserves slash compatibility', () => {
    const dispatchSlash = vi.fn()

    setActionContextProvider(() => ({ ...ctx, dispatchSlash }))
    executeAction(ACTION_REGISTRY.get('skills.open')!)

    expect(dispatchSlash).toHaveBeenCalledWith('/skills')
  })

  it('explains why unsafe actions are disabled', () => {
    const dashboardCtx = { ...ctx, dashboard: true }
    const quit = ACTION_REGISTRY.search('quit', dashboardCtx)[0]

    expect(quit?.availability).toEqual({
      reason: 'Quit is disabled in hosted dashboard chat',
      status: 'disabled'
    })
  })
})
