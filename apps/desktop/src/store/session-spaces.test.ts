import { afterEach, describe, expect, it, vi } from 'vitest'

const state = vi.hoisted(() => ({
  createSessionSpace: vi.fn(),
  listSessionSpaces: vi.fn(),
  sessions: [{ id: 's1', space_id: null as null | string }],
  setSessionSpace: vi.fn()
}))

vi.mock('@/hermes', () => ({
  createSessionSpace: state.createSessionSpace,
  getApiRequestConnection: () => null,
  listSessionSpaces: state.listSessionSpaces,
  profileScopeKey: (scope?: { connectionId?: null | string; profile?: null | string }) => {
    const profile = scope?.profile?.trim() || 'default'
    const connectionId = scope?.connectionId?.trim()

    return connectionId && connectionId !== 'local' ? `${connectionId}::${profile}` : profile
  },
  setSessionSpace: state.setSessionSpace
}))

vi.mock('./session', () => ({
  setSessions: (update: (sessions: typeof state.sessions) => typeof state.sessions) => {
    state.sessions = update(state.sessions)
  }
}))

import {
  $sessionSpacesByScope,
  assignSessionSpace,
  createAndAssignSessionSpace,
  refreshSessionSpaces,
  sessionSpacesForScope
} from './session-spaces'

const space = {
  created_at: 1,
  id: 'space_1',
  name: 'Infrastructure',
  updated_at: 1
}

afterEach(() => {
  state.createSessionSpace.mockReset()
  state.listSessionSpaces.mockReset()
  state.setSessionSpace.mockReset()
  state.sessions = [{ id: 's1', space_id: null }]
  $sessionSpacesByScope.set({})
})

describe('session spaces', () => {
  it('hydrates the backend-owned registry', async () => {
    state.listSessionSpaces.mockResolvedValue({ spaces: [space] })

    await refreshSessionSpaces('default')

    expect(state.listSessionSpaces).toHaveBeenCalledWith('default')
    expect(sessionSpacesForScope('default')).toEqual([space])
  })

  it('keeps profile registries isolated when refreshes resolve out of order', async () => {
    let resolveDefault!: (value: { spaces: typeof space[] }) => void
    let resolveWork!: (value: { spaces: typeof space[] }) => void
    const workSpace = { ...space, id: 'space_work', name: 'Work' }

    state.listSessionSpaces.mockImplementation((profile: string) =>
      new Promise(resolve => {
        if (profile === 'work') {
          resolveWork = resolve
        } else {
          resolveDefault = resolve
        }
      })
    )

    const defaultRefresh = refreshSessionSpaces('default')
    const workRefresh = refreshSessionSpaces('work')

    resolveWork({ spaces: [workSpace] })
    await workRefresh
    resolveDefault({ spaces: [space] })
    await defaultRefresh

    expect(sessionSpacesForScope('default')).toEqual([space])
    expect(sessionSpacesForScope('work')).toEqual([workSpace])
  })

  it('rolls an optimistic assignment back when persistence fails', async () => {
    state.setSessionSpace.mockRejectedValue(new Error('offline'))

    await expect(assignSessionSpace('s1', 'space_1')).rejects.toThrow('offline')

    expect(state.sessions[0].space_id).toBeNull()
  })

  it('creates a space and assigns the session without touching cwd', async () => {
    state.createSessionSpace.mockResolvedValue({ space })
    state.setSessionSpace.mockResolvedValue({ ok: true })

    await createAndAssignSessionSpace('s1', 'Infrastructure')

    expect(state.createSessionSpace).toHaveBeenCalledWith({ name: 'Infrastructure' }, undefined)
    expect(state.setSessionSpace).toHaveBeenCalledWith('s1', 'space_1', undefined)
    expect(state.sessions[0].space_id).toBe('space_1')
    expect(sessionSpacesForScope()).toEqual([space])
  })
})
