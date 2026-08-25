import { beforeEach, describe, expect, it, vi } from 'vitest'

import { startFocusedWorktreeSession } from './worktree-session'

const { focusedStoredSessionId, knownOwnerForSession, requestStartWorkSession } = vi.hoisted(() => ({
  focusedStoredSessionId: { get: vi.fn() },
  knownOwnerForSession: vi.fn(),
  requestStartWorkSession: vi.fn()
}))

vi.mock('@/store/projects', () => ({ requestStartWorkSession }))
vi.mock('@/store/session-states', () => ({
  $focusedStoredSessionId: focusedStoredSessionId,
  knownOwnerForSession
}))

describe('startFocusedWorktreeSession', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    focusedStoredSessionId.get.mockReturnValue('remote-session')
  })

  it('publishes the exact owner of the focused surface', () => {
    const owner = {
      connectionId: 'source-a',
      mode: 'remote' as const,
      profile: 'worker',
      targetProfile: 'backend-worker'
    }

    knownOwnerForSession.mockReturnValue(owner)

    startFocusedWorktreeSession('/repo/.worktrees/tests')

    expect(knownOwnerForSession).toHaveBeenCalledWith('remote-session')
    expect(requestStartWorkSession).toHaveBeenCalledWith('/repo/.worktrees/tests', undefined, { owner })
  })
})
