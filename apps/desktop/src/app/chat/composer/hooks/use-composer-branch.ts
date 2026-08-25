import { type MutableRefObject, useCallback } from 'react'

import { listRepoBranches, requestStartWorkSession, startWorkInRepo, switchBranchInRepo } from '@/store/projects'
import { $selectedStoredSessionId } from '@/store/session'
import type { SessionOwnerScope } from '@/store/session-request-router'
import { knownOwnerForSession } from '@/store/session-states'

import { useComposerScope } from '../scope'

interface UseComposerBranchOptions {
  clearDraft: () => void
  cwd: null | string | undefined
  draftRef: MutableRefObject<string>
}

/**
 * Branch / worktree engine — the `CodingStatusRow` hand-offs. Each action opens
 * a fresh session anchored in a worktree carrying the current composer draft as
 * its first turn; clearing here means the draft travels to the new session
 * instead of getting stashed under this one. Backend coupling (cwd + the
 * projects store) is the only dependency; nothing about ChatBar's render.
 */
export function useComposerBranch({ clearDraft, cwd, draftRef }: UseComposerBranchOptions) {
  const scope = useComposerScope()

  const surfaceOwner = useCallback(() => {
    if (scope.owner) {
      return scope.owner
    }

    const storedSessionId =
      scope.target === 'main'
        ? $selectedStoredSessionId.get()
        : scope.target.startsWith('tile:')
          ? scope.target.slice('tile:'.length)
          : null

    return knownOwnerForSession(storedSessionId)
  }, [scope.owner, scope.target])

  const handOffWorktree = useCallback(
    (path: string, owner: SessionOwnerScope) => {
      const text = draftRef.current

      clearDraft()
      scope.attachments.clear()
      requestStartWorkSession(path, text, owner ? { owner } : undefined)
    },
    [clearDraft, draftRef, scope.attachments]
  )

  // Hand a worktree off to the controller: open a fresh session anchored there,
  // carrying the composer draft as its first turn. Clearing here means the draft
  // travels to the new session instead of getting stashed under this one.
  const openInWorktree = useCallback(
    (path: string) => handOffWorktree(path, surfaceOwner()),
    [handOffWorktree, surfaceOwner]
  )

  // Branch off into a NEW worktree (base = branch name, or current HEAD). A
  // create failure throws back to the row (which toasts) before we touch the
  // draft; a missing cwd / remote backend no-ops (the row hides the affordance).
  const handleBranchOff = useCallback(
    async (branch: string, base?: string) => {
      const repoPath = cwd?.trim()
      const owner = surfaceOwner()
      const result = repoPath && (await startWorkInRepo(repoPath, { base, branch, name: branch }))

      if (result) {
        handOffWorktree(result.path, owner)
      }
    },
    [cwd, handOffWorktree, surfaceOwner]
  )

  // Convert an EXISTING branch into a fresh worktree + session (no new branch).
  // Mirrors handleBranchOff's hand-off: create the worktree, then open a session
  // anchored there carrying the draft.
  const handleConvertBranch = useCallback(
    async (branch: string, path?: null | string, isDefault?: boolean) => {
      const owner = surfaceOwner()

      if (path?.trim()) {
        handOffWorktree(path, owner)

        return
      }

      const repoPath = cwd?.trim()

      if (repoPath && isDefault) {
        await switchBranchInRepo(repoPath, branch)
        handOffWorktree(repoPath, owner)

        return
      }

      const result = repoPath && (await startWorkInRepo(repoPath, { existingBranch: branch }))

      if (result) {
        handOffWorktree(result.path, owner)
      }
    },
    [cwd, handOffWorktree, surfaceOwner]
  )

  const handleListBranches = useCallback(async () => {
    const repoPath = cwd?.trim()

    return repoPath ? listRepoBranches(repoPath) : []
  }, [cwd])

  const handleSwitchBranch = useCallback(
    async (branch: string) => {
      const repoPath = cwd?.trim()

      if (repoPath) {
        await switchBranchInRepo(repoPath, branch)
      }
    },
    [cwd]
  )

  return { handleBranchOff, handleConvertBranch, handleListBranches, handleSwitchBranch, openInWorktree }
}
