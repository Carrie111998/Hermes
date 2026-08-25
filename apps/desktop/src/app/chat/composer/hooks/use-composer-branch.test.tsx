import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { $selectedStoredSessionId } from '@/store/session'

import { ComposerScopeProvider, MAIN_COMPOSER_SCOPE } from '../scope'

import { useComposerBranch } from './use-composer-branch'

const { knownOwnerForSession, requestStartWorkSession, startWorkInRepo } = vi.hoisted(() => ({
  knownOwnerForSession: vi.fn(),
  requestStartWorkSession: vi.fn(),
  startWorkInRepo: vi.fn()
}))

vi.mock('@/store/projects', () => ({
  listRepoBranches: vi.fn(async () => []),
  requestStartWorkSession,
  startWorkInRepo,
  switchBranchInRepo: vi.fn()
}))

vi.mock('@/store/session-states', () => ({
  knownOwnerForSession
}))

describe('useComposerBranch worktree ownership', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    $selectedStoredSessionId.set('main-source')
  })

  it('carries a tiled composer profile route into the fresh worktree session', () => {
    const route = {
      connectionId: 'remote-a',
      mode: 'remote' as const,
      profile: 'itb',
      targetProfile: 'backend-itb'
    }

    const wrapper = ({ children }: { children: ReactNode }) => (
      <ComposerScopeProvider value={{ ...MAIN_COMPOSER_SCOPE, owner: route }}>{children}</ComposerScopeProvider>
    )

    const clearDraft = vi.fn()
    const draftRef = { current: 'close the remaining test gaps' }
    const { result } = renderHook(() => useComposerBranch({ clearDraft, cwd: '/repo', draftRef }), { wrapper })

    act(() => result.current.openInWorktree('/repo/.worktrees/tests'))

    expect(clearDraft).toHaveBeenCalledTimes(1)
    expect(requestStartWorkSession).toHaveBeenCalledWith('/repo/.worktrees/tests', 'close the remaining test gaps', {
      owner: route
    })
  })

  it('captures the exact focused main session owner when the composer has no explicit route', () => {
    const owner = { connectionId: 'source-main', profile: 'itb' }

    knownOwnerForSession.mockReturnValue(owner)

    const clearDraft = vi.fn()
    const draftRef = { current: 'continue in a worktree' }
    const { result } = renderHook(() => useComposerBranch({ clearDraft, cwd: '/repo', draftRef }))

    act(() => result.current.openInWorktree('/repo/.worktrees/main-tests'))

    expect(knownOwnerForSession).toHaveBeenCalledWith('main-source')
    expect(requestStartWorkSession).toHaveBeenLastCalledWith('/repo/.worktrees/main-tests', 'continue in a worktree', {
      owner
    })
  })

  it('keeps the main session owner captured before asynchronous worktree creation', async () => {
    let resolveWorktree!: (value: { branch: string; path: string }) => void
    const sourceOwner = { connectionId: 'source-main', profile: 'itb' }
    const otherOwner = { connectionId: 'source-tile', profile: 'default' }

    knownOwnerForSession.mockReturnValueOnce(sourceOwner).mockReturnValue(otherOwner)
    startWorkInRepo.mockReturnValueOnce(
      new Promise(resolve => {
        resolveWorktree = resolve
      })
    )

    const clearDraft = vi.fn()
    const draftRef = { current: 'continue after creating the worktree' }
    const { result } = renderHook(() => useComposerBranch({ clearDraft, cwd: '/repo', draftRef }))

    let handoff!: Promise<void>

    act(() => {
      handoff = result.current.handleBranchOff('tests')
    })

    $selectedStoredSessionId.set('other-session')
    resolveWorktree({ branch: 'tests', path: '/repo/.worktrees/tests' })

    await act(async () => handoff)

    expect(knownOwnerForSession).toHaveBeenCalledTimes(1)
    expect(knownOwnerForSession).toHaveBeenCalledWith('main-source')
    expect(requestStartWorkSession).toHaveBeenCalledWith(
      '/repo/.worktrees/tests',
      'continue after creating the worktree',
      { owner: sourceOwner }
    )
  })

  it('keeps an unresolved owner unresolved across asynchronous worktree creation', async () => {
    let resolveWorktree!: (value: { branch: string; path: string }) => void

    knownOwnerForSession.mockReturnValueOnce(undefined).mockReturnValue({
      connectionId: 'late-source',
      profile: 'default'
    })
    startWorkInRepo.mockReturnValueOnce(
      new Promise(resolve => {
        resolveWorktree = resolve
      })
    )

    const { result } = renderHook(() =>
      useComposerBranch({ clearDraft: vi.fn(), cwd: '/repo', draftRef: { current: 'stay unowned' } })
    )

    let handoff!: Promise<void>

    act(() => {
      handoff = result.current.handleBranchOff('tests')
    })

    $selectedStoredSessionId.set('late-session')
    resolveWorktree({ branch: 'tests', path: '/repo/.worktrees/tests' })
    await act(async () => handoff)

    expect(knownOwnerForSession).toHaveBeenCalledTimes(1)
    expect(requestStartWorkSession).toHaveBeenCalledWith('/repo/.worktrees/tests', 'stay unowned', undefined)
  })
})
