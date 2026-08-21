import { afterEach, describe, expect, it, vi } from 'vitest'

const state = vi.hoisted(() => ({
  createSessionSpace: vi.fn(),
  listSessionSpaces: vi.fn(),
  sessions: [{ id: 's1', space_id: null as null | string }],
  setSessionSpace: vi.fn()
}))

vi.mock('@/hermes', () => ({
  createSessionSpace: state.createSessionSpace,
  listSessionSpaces: state.listSessionSpaces,
  setSessionSpace: state.setSessionSpace
}))

vi.mock('./session', () => ({
  setSessions: (update: (sessions: typeof state.sessions) => typeof state.sessions) => {
    state.sessions = update(state.sessions)
  }
}))

import {
  $sessionSpaces,
  assignSessionSpace,
  createAndAssignSessionSpace,
  refreshSessionSpaces
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
  $sessionSpaces.set([])
})

describe('session spaces', () => {
  it('hydrates the backend-owned registry', async () => {
    state.listSessionSpaces.mockResolvedValue({ spaces: [space] })

    await refreshSessionSpaces('default')

    expect(state.listSessionSpaces).toHaveBeenCalledWith('default')
    expect($sessionSpaces.get()).toEqual([space])
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
  })
})
