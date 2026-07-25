import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesRepoStatus } from '@/global'
import { $sessionsLimit, resetSessionsLimit, SIDEBAR_SESSIONS_PAGE_SIZE } from '@/store/layout'
import {
  $cronSessions,
  $freshDraftReady,
  $messagingSessions,
  $sessionProfilesTruncated,
  $sessions,
  $sessionsLoading,
  setCronSessions,
  setFreshDraftReady,
  setMessagingSessions,
  setSessionProfilesTruncated,
  setSessions,
  setSessionsLoading
} from '@/store/session'
import { $stalledSessionIds } from '@/store/session-states'

import { $repoStatus, $repoWorktrees, refreshRepoStatus } from './coding-status'
import { $gatewaySwitching, wipeSessionListsForGatewaySwitch } from './gateway-switch'
import {
  $currentCwd,
  $selectedStoredSessionId,
  $workspaceCwdOwner,
  releaseWorkspaceCwdOwner,
  setSelectedStoredSessionId,
  setWorkspaceCwdOwner,
  workspaceCwdBelongsToSelectedSession
} from './session'

vi.mock('@/lib/query-client', () => ({
  invalidateProfileScopedQueries: vi.fn()
}))

describe('wipeSessionListsForGatewaySwitch', () => {
  beforeEach(() => {
    $gatewaySwitching.set(false)
    setSessions([{ id: 's1', title: 'old', profile: 'default' } as never])
    setSessionProfilesTruncated({ default: true })
    setCronSessions([{ id: 'c1', title: 'cron', profile: 'default' } as never])
    setMessagingSessions([{ id: 'm1', title: 'tg', profile: 'default' } as never])
    $stalledSessionIds.set(['s1'])
    setSessionsLoading(false)
    setFreshDraftReady(false)
    $sessionsLimit.set(SIDEBAR_SESSIONS_PAGE_SIZE * 3)
  })

  afterEach(() => {
    resetSessionsLimit()
    setSessions([])
    setCronSessions([])
    setMessagingSessions([])
    $stalledSessionIds.set([])
    setSessionsLoading(true)
    $gatewaySwitching.set(false)
  })

  it('clears lists and arms loading so sidebar skeletons retrigger', () => {
    wipeSessionListsForGatewaySwitch()

    expect($sessions.get()).toEqual([])
    expect($sessionProfilesTruncated.get()).toEqual({})
    expect($cronSessions.get()).toEqual([])
    expect($messagingSessions.get()).toEqual([])
    expect($stalledSessionIds.get()).toEqual([])
    expect($sessionsLoading.get()).toBe(true)
    expect($sessionsLimit.get()).toBe(SIDEBAR_SESSIONS_PAGE_SIZE)
    expect($freshDraftReady.get()).toBe(true)
  })
})

// A soft switch de-selects the conversation but deliberately leaves $currentCwd
// alone (the user stays where they were, e.g. mid-Gateway settings). Workspace
// probes gate on "does the SELECTED conversation own $currentCwd", and the
// withheld branch CLEARS the coding rail — so a wipe that stranded the owner
// naming the previous backend's conversation would blank the rail forever:
// nothing in this path re-selects a conversation, and seedDefaultCwd is gated on
// an empty cwd (#71254).
describe('wipeSessionListsForGatewaySwitch workspace ownership', () => {
  const sampleStatus = { branch: 'main', changed: 0, files: [] } as unknown as HermesRepoStatus

  beforeEach(() => {
    vi.useFakeTimers()
    $repoStatus.set(null)
    $repoWorktrees.set([])
    $currentCwd.set('')
    setSelectedStoredSessionId(null)
    setWorkspaceCwdOwner(null)
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
  })

  afterEach(() => {
    vi.clearAllTimers()
    vi.useRealTimers()
    $repoStatus.set(null)
    $currentCwd.set('')
    setSelectedStoredSessionId(null)
    setWorkspaceCwdOwner(null)
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
  })

  it('hands the workspace back to the draft state so probes are not stranded', () => {
    $currentCwd.set('/repo-a')
    setSelectedStoredSessionId('session-a')
    setWorkspaceCwdOwner('session-a')

    wipeSessionListsForGatewaySwitch()

    // Both null is the draft state the design treats as owned. Asserting the
    // predicate (not the atom value) is the contract: after the wipe the
    // workspace must be usable by whatever the switch lands on.
    expect($selectedStoredSessionId.get()).toBeNull()
    expect(workspaceCwdBelongsToSelectedSession()).toBe(true)
  })

  it('leaves a defaulted repo-status refresh able to probe and publish after the wipe', async () => {
    const probe = vi.fn(async () => sampleStatus)

    ;(window as unknown as { hermesDesktop?: unknown }).hermesDesktop = { git: { repoStatus: probe } }

    $currentCwd.set('/repo-a')
    setSelectedStoredSessionId('session-a')
    setWorkspaceCwdOwner('session-a')

    wipeSessionListsForGatewaySwitch()

    // The wipe keeps the path on purpose, so this is the state every later
    // DEFAULTED edge (turn settle, window focus, worktree token) runs in. It has
    // to probe: a withheld refresh here clears the rail with nothing left to
    // re-arm it.
    expect($currentCwd.get()).toBe('/repo-a')
    await refreshRepoStatus()

    expect(probe).toHaveBeenCalledWith('/repo-a')
    expect($repoStatus.get()).toEqual(sampleStatus)
  })

  it('does not strand ownership when the wipe runs from an unowned workspace', () => {
    // Resuming a detached conversation releases the workspace to the private
    // unowned marker, reached through the same call the resume path uses so this
    // runs on the real marker rather than a hand-copied literal. A wipe from THAT
    // state must also land owned, otherwise the mismatch survives the switch just
    // as it did when the owner named a conversation.
    $currentCwd.set('/repo-a')
    setSelectedStoredSessionId('session-detached')
    releaseWorkspaceCwdOwner()
    expect(workspaceCwdBelongsToSelectedSession()).toBe(false)

    wipeSessionListsForGatewaySwitch()

    expect($workspaceCwdOwner.get()).toBeNull()
    expect(workspaceCwdBelongsToSelectedSession()).toBe(true)
  })
})
