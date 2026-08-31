import { act, cleanup, renderHook } from '@testing-library/react'
import { type PropsWithChildren } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ComposerScopeProvider, MAIN_COMPOSER_SCOPE } from '../scope'

import { useComposerBranch } from './use-composer-branch'

const mocks = vi.hoisted(() => ({
  listRepoBranches: vi.fn(async () => []),
  requestStartWorkSession: vi.fn(),
  startWorkInRepo: vi.fn(),
  switchBranchInRepo: vi.fn(async () => undefined)
}))

vi.mock('@/store/projects', () => mocks)

function Wrapper({ children }: PropsWithChildren) {
  return <ComposerScopeProvider value={MAIN_COMPOSER_SCOPE}>{children}</ComposerScopeProvider>
}

describe('useComposerBranch session-transition lifecycle', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('does not clear B or open a session when an A worktree request resolves late', async () => {
    let resolveWorktree: ((result: { path: string }) => void) | undefined

    const pendingWorktree = new Promise<{ path: string }>(resolve => {
      resolveWorktree = resolve
    })

    mocks.startWorkInRepo.mockReturnValueOnce(pendingWorktree)
    const clearDraft = vi.fn()
    const draftRef = { current: 'draft from A' }

    const hook = renderHook(
      ({ actionsDisabled, sessionKey }: { actionsDisabled: boolean; sessionKey: string }) =>
        useComposerBranch({
          actionsDisabled,
          clearDraft,
          cwd: `/repo/${sessionKey}`,
          draftRef,
          sessionKey
        }),
      { initialProps: { actionsDisabled: false, sessionKey: 'session-a' }, wrapper: Wrapper }
    )

    let branchPromise: Promise<void> | undefined

    act(() => {
      branchPromise = hook.result.current.handleBranchOff('feature-a')
    })

    hook.rerender({ actionsDisabled: true, sessionKey: 'session-b' })
    draftRef.current = 'draft from B'
    hook.rerender({ actionsDisabled: false, sessionKey: 'session-b' })

    await act(async () => {
      resolveWorktree?.({ path: '/worktrees/feature-a' })
      await branchPromise
    })

    expect(clearDraft).not.toHaveBeenCalled()
    expect(mocks.requestStartWorkSession).not.toHaveBeenCalled()
    expect(draftRef.current).toBe('draft from B')
  })
})
