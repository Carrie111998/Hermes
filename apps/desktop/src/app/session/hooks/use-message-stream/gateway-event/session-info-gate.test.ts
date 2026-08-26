import type { ReadableAtom } from 'nanostores'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import type * as sessionStore from '@/store/session'

// Workspace-identity gate on the foreground publish (#92888): a background
// Kanban worker's session.info carries ITS PR-worktree cwd/branch. The cwd
// write was already gated on the event describing the selected session; the
// branch write was not, so the default chat's coding rail pointed at another
// session's checkout while the conversation never moved.

const { current } = vi.hoisted(() => ({
  current: {
    selectedStoredSessionId: null as null | string,
    sessions: [] as SessionInfo[]
  }
}))

vi.mock('@/store/session', async importOriginal => {
  const actual = await importOriginal<typeof sessionStore>()

  return {
    ...actual,
    $selectedStoredSessionId: { get: () => current.selectedStoredSessionId },
    $sessions: { get: () => current.sessions }
  }
})

import { workspaceIdentityMatchesSelectedSession } from './session-info-gate'
import type { GateContext } from './session-info-gate'

const ctx = (): GateContext => ({
  $selectedStoredSessionId: { get: () => current.selectedStoredSessionId } as unknown as ReadableAtom<null | string>,
  $sessions: { get: () => current.sessions } as unknown as ReadableAtom<SessionInfo[]>
})

describe('workspace-identity gate for foreground runtime publishes (#92888)', () => {
  beforeEach(() => {
    current.selectedStoredSessionId = 'stored-default'
    current.sessions = [{ id: 'stored-default' } as SessionInfo]
  })

  it('accepts an event whose stored id IS the selection', () => {
    expect(workspaceIdentityMatchesSelectedSession('stored-default', ctx())).toBe(true)
  })

  it('accepts an absent stored id (lazy/not-yet-built session — #71254 contract)', () => {
    expect(workspaceIdentityMatchesSelectedSession(undefined, ctx())).toBe(true)
    expect(workspaceIdentityMatchesSelectedSession('', ctx())).toBe(true)
  })

  it('rejects a DIFFERENT named session (the Kanban worker worktree case)', () => {
    expect(workspaceIdentityMatchesSelectedSession('worker-pr-worktree', ctx())).toBe(false)
  })

  it('rejects any named session when the selection is a fresh draft (null)', () => {
    current.selectedStoredSessionId = null

    // Even the worker's own id must not rehome a draft.
    expect(workspaceIdentityMatchesSelectedSession('worker-pr-worktree', ctx())).toBe(false)
  })

  it('still matches across a compression rotation via the lineage', () => {
    current.selectedStoredSessionId = 'root-id'
    current.sessions = [
      { id: 'tip-id', _lineage_root_id: 'root-id' },
      { id: 'root-id' }
    ] as unknown as SessionInfo[]

    expect(workspaceIdentityMatchesSelectedSession('tip-id', ctx())).toBe(true)
  })
})
