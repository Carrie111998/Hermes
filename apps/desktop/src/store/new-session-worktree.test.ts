import { describe, expect, it, vi } from 'vitest'

import type { HermesGitBaseBranch, HermesGitWorktree, HermesRepoStatus } from '@/global'

import { isolateNewSessionCwd } from './new-session-worktree'

function gitBridge(overrides: Record<string, unknown> = {}) {
  return {
    baseBranchList: vi.fn(async (): Promise<HermesGitBaseBranch[]> => [
      { isDefault: true, isRemote: true, name: 'origin/main' }
    ]),
    repoStatus: vi.fn(async (): Promise<HermesRepoStatus | null> =>
      ({ branch: 'feature/stale', defaultBranch: 'main', detached: false } as HermesRepoStatus)
    ),
    worktreeAdd: vi.fn(async () => ({ branch: 'hermes/session-fixed', path: '/repo/.worktrees/session-fixed' })),
    worktreeList: vi.fn(async (): Promise<HermesGitWorktree[]> => [
      { branch: 'feature/stale', detached: false, isMain: true, locked: false, path: '/repo' }
    ]),
    ...overrides
  }
}

describe('isolateNewSessionCwd', () => {
  it('creates a fresh worktree from the default branch instead of inheriting the main checkout branch', async () => {
    const git = gitBridge()

    const cwd = await isolateNewSessionCwd('/repo', git, () => 'fixed')

    expect(cwd).toBe('/repo/.worktrees/session-fixed')
    expect(git.worktreeAdd).toHaveBeenCalledWith('/repo', {
      base: 'origin/main',
      branch: 'hermes/session-fixed',
      name: 'session-fixed'
    })
  })

  it('falls back to the default branch detected by repo status', async () => {
    const git = gitBridge({ baseBranchList: vi.fn(async () => []) })

    await isolateNewSessionCwd('/repo', git, () => 'status-default')

    expect(git.worktreeAdd).toHaveBeenCalledWith('/repo', {
      base: 'main',
      branch: 'hermes/session-status-default',
      name: 'session-status-default'
    })
  })

  it('fails closed when Git cannot detect a default branch', async () => {
    const git = gitBridge({
      baseBranchList: vi.fn(async () => []),
      repoStatus: vi.fn(async () => ({ branch: 'feature/stale', defaultBranch: null, detached: false } as HermesRepoStatus))
    })

    await expect(isolateNewSessionCwd('/repo', git, () => 'no-default')).rejects.toThrow(
      'Cannot isolate a new session without a default branch'
    )
    expect(git.worktreeAdd).not.toHaveBeenCalled()
  })

  it('keeps an explicitly targeted linked worktree', async () => {
    const git = gitBridge({
      worktreeList: vi.fn(async (): Promise<HermesGitWorktree[]> => [
        { branch: 'main', detached: false, isMain: true, locked: false, path: '/repo' },
        {
          branch: 'feature/existing',
          detached: false,
          isMain: false,
          locked: false,
          path: '/repo/.worktrees/existing'
        }
      ])
    })

    const cwd = await isolateNewSessionCwd('/repo/.worktrees/existing', git, () => 'unused')

    expect(cwd).toBe('/repo/.worktrees/existing')
    expect(git.worktreeAdd).not.toHaveBeenCalled()
  })

  it('leaves a non-git directory unchanged', async () => {
    const git = gitBridge({ repoStatus: vi.fn(async () => null) })

    const cwd = await isolateNewSessionCwd('/notes', git, () => 'unused')

    expect(cwd).toBe('/notes')
    expect(git.worktreeList).not.toHaveBeenCalled()
    expect(git.worktreeAdd).not.toHaveBeenCalled()
  })

  it('recognizes a Windows subdirectory of the main worktree case-insensitively', async () => {
    const git = gitBridge({
      worktreeList: vi.fn(async (): Promise<HermesGitWorktree[]> => [
        { branch: 'feature/stale', detached: false, isMain: true, locked: false, path: 'C:\\Repo' }
      ])
    })

    await isolateNewSessionCwd('c:/repo/packages/app', git, () => 'windows')

    expect(git.worktreeAdd).toHaveBeenCalledWith('C:\\Repo', {
      base: 'origin/main',
      branch: 'hermes/session-windows',
      name: 'session-windows'
    })
  })

  it('recognizes a slash-form Windows UNC path case-insensitively', async () => {
    const git = gitBridge({
      worktreeList: vi.fn(async (): Promise<HermesGitWorktree[]> => [
        { branch: 'feature/stale', detached: false, isMain: true, locked: false, path: '//Server/Share/Repo' }
      ])
    })

    await isolateNewSessionCwd('//server/share/repo/packages/app', git, () => 'unc')

    expect(git.worktreeAdd).toHaveBeenCalledWith('//Server/Share/Repo', {
      base: 'origin/main',
      branch: 'hermes/session-unc',
      name: 'session-unc'
    })
  })

  it('reports ownership metadata for an automatically created worktree', async () => {
    const git = gitBridge()
    const onCreated = vi.fn()

    await isolateNewSessionCwd('/repo', git, () => 'owned', onCreated)

    expect(onCreated).toHaveBeenCalledWith({
      base: 'origin/main',
      branch: 'hermes/session-fixed',
      path: '/repo/.worktrees/session-fixed',
      repoPath: '/repo'
    })
  })

  it('preserves a case-distinct linked worktree on POSIX', async () => {
    const git = gitBridge({
      worktreeList: vi.fn(async (): Promise<HermesGitWorktree[]> => [
        { branch: 'main', detached: false, isMain: true, locked: false, path: '/Repo' },
        { branch: 'feature/existing', detached: false, isMain: false, locked: false, path: '/repo' }
      ])
    })

    const cwd = await isolateNewSessionCwd('/repo', git, () => 'unused')

    expect(cwd).toBe('/repo')
    expect(git.worktreeAdd).not.toHaveBeenCalled()
  })

  it('propagates worktree creation failures instead of falling back to the canonical checkout', async () => {
    const git = gitBridge({ worktreeAdd: vi.fn(async () => Promise.reject(new Error('git worktree add failed'))) })

    await expect(isolateNewSessionCwd('/repo', git, () => 'failure')).rejects.toThrow('git worktree add failed')
  })
})
