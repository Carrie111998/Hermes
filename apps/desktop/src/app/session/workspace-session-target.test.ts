import { afterEach, describe, expect, it, vi } from 'vitest'

import type { NewChatOwner } from '@/store/profile'
import {
  $projectScope,
  $projectTree,
  $startWorkSessionRequest,
  ALL_PROJECTS,
  requestStartWorkSession
} from '@/store/projects'
import {
  $currentBranch,
  $currentCwd,
  $newChatWorkspaceTarget,
  type NewChatWorkspaceTarget,
  setCurrentBranch,
  setCurrentCwd,
  setNewChatWorkspaceTarget
} from '@/store/session'

import { deferred } from '../../test/deferred'

import { handleStartWorkSessionRequest, startWorkspaceSession } from './workspace-session-target'

describe('handleStartWorkSessionRequest', () => {
  afterEach(() => $startWorkSessionRequest.set(null))

  it('passes the published owner/openTab/draft through the controller bridge', () => {
    const owner = {
      connectionId: 'source-a',
      mode: 'remote' as const,
      profile: 'itb',
      targetProfile: 'backend-itb'
    }

    requestStartWorkSession('C:/repo/.worktrees/tests', 'continue the tests', { openTab: true, owner })
    const request = $startWorkSessionRequest.get()
    const startSessionInWorkspace = vi.fn()
    const insertDraft = vi.fn()

    handleStartWorkSessionRequest(request!, startSessionInWorkspace, insertDraft)

    expect(startSessionInWorkspace).toHaveBeenCalledWith('C:/repo/.worktrees/tests', { openTab: true, owner })
    expect(insertDraft).toHaveBeenCalledWith('continue the tests')
  })
})

describe('startWorkspaceSession', () => {
  afterEach(() => {
    setCurrentBranch('')
    setCurrentCwd('')
    setNewChatWorkspaceTarget(undefined)
    $projectScope.set(ALL_PROJECTS)
    $projectTree.set([])
    vi.restoreAllMocks()
  })

  it('keeps a newer sidebar target when an older project lookup resolves', async () => {
    const first = deferred<{ branch?: string; cwd?: string }>()
    const second = deferred<{ branch?: string; cwd?: string }>()

    const requestGateway = vi
      .fn()
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise)

    const activeSessionIdRef = { current: null }

    const startFreshSessionDraft = vi.fn(
      (options?: { owner?: NewChatOwner; workspaceTarget?: NewChatWorkspaceTarget }) => {
        setNewChatWorkspaceTarget(options?.workspaceTarget)
        setCurrentCwd(options?.workspaceTarget || '')
      }
    )

    const followActiveSessionCwd = vi.fn()

    startWorkspaceSession({
      activeSessionIdRef,
      followActiveSessionCwd,
      path: '/workspace-a',
      requestGateway,
      startFreshSessionDraft
    })
    startWorkspaceSession({
      activeSessionIdRef,
      followActiveSessionCwd,
      path: '/workspace-b',
      requestGateway,
      startFreshSessionDraft
    })

    first.resolve({ branch: 'stale', cwd: '/normalized-a' })
    await first.promise
    await Promise.resolve()

    expect($newChatWorkspaceTarget.get()).toBe('/workspace-b')
    expect($currentCwd.get()).toBe('/workspace-b')
    expect($currentBranch.get()).not.toBe('stale')

    second.resolve({ branch: 'main', cwd: '/normalized-b' })
    await second.promise
    await Promise.resolve()

    expect($newChatWorkspaceTarget.get()).toBe('/normalized-b')
    expect($currentCwd.get()).toBe('/normalized-b')
    expect($currentBranch.get()).toBe('main')
  })

  it('keeps a Home new-session request detached even when another project scope is active', () => {
    $projectScope.set('p_voice')
    $projectTree.set([
      {
        id: 'p_voice',
        label: 'Voice Assistant',
        path: '/Users/oschmidt/Checkouts/voice-assistant',
        repos: [],
        sessionCount: 0
      }
    ])

    const requestGateway = vi.fn()
    const activeSessionIdRef = { current: null }

    const startFreshSessionDraft = vi.fn(
      (options?: { owner?: NewChatOwner; workspaceTarget?: NewChatWorkspaceTarget }) => {
        setNewChatWorkspaceTarget(options?.workspaceTarget)
        setCurrentCwd(options?.workspaceTarget || '')
      }
    )

    startWorkspaceSession({
      activeSessionIdRef,
      path: null,
      requestGateway,
      startFreshSessionDraft
    })

    expect(startFreshSessionDraft).toHaveBeenCalledWith({ workspaceTarget: null })
    expect(requestGateway).not.toHaveBeenCalled()
    expect($newChatWorkspaceTarget.get()).toBeNull()
    expect($currentCwd.get()).toBe('')
  })

  it('carries the source owner into the fresh worktree draft', () => {
    const startFreshSessionDraft = vi.fn()

    startWorkspaceSession({
      activeSessionIdRef: { current: null },
      owner: 'itb',
      path: 'C:/repo/.worktrees/tests',
      requestGateway: vi.fn(async () => ({}) as never),
      startFreshSessionDraft
    })

    expect(startFreshSessionDraft).toHaveBeenCalledWith({
      owner: 'itb',
      workspaceTarget: 'C:/repo/.worktrees/tests'
    })
  })
})
