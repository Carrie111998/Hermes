import { type MutableRefObject, useCallback, useLayoutEffect, useRef } from 'react'

import { listRepoBranches, requestStartWorkSession, startWorkInRepo, switchBranchInRepo } from '@/store/projects'

import { useComposerScope } from '../scope'

import { useCommittedActionScope } from './use-committed-action-scope'

interface UseComposerBranchOptions {
  actionsDisabled: boolean
  clearDraft: () => void
  cwd: null | string | undefined
  draftRef: MutableRefObject<string>
  sessionKey: string | null
}

/**
 * Branch / worktree engine — the `CodingStatusRow` hand-offs. Each action opens
 * a fresh session anchored in a worktree carrying the current composer draft as
 * its first turn; clearing here means the draft travels to the new session
 * instead of getting stashed under this one. Backend coupling (cwd + the
 * projects store) is the only dependency; nothing about ChatBar's render.
 */
export function useComposerBranch({
  actionsDisabled,
  clearDraft,
  cwd,
  draftRef,
  sessionKey
}: UseComposerBranchOptions) {
  const scope = useComposerScope()
  const actionIsCurrent = useCommittedActionScope(sessionKey, actionsDisabled)
  const actionStateRef = useRef({ actionsDisabled, clearDraft, cwd, draftRef })

  useLayoutEffect(() => {
    actionStateRef.current = { actionsDisabled, clearDraft, cwd, draftRef }
  }, [actionsDisabled, clearDraft, cwd, draftRef])

  // Hand a worktree off to the controller: open a fresh session anchored there,
  // carrying the composer draft as its first turn. Clearing here means the draft
  // travels to the new session instead of getting stashed under this one.
  const openInWorktree = useCallback(
    (path: string) => {
      if (!actionIsCurrent()) {
        return
      }

      const action = actionStateRef.current
      const text = action.draftRef.current

      action.clearDraft()
      scope.attachments.clear()
      requestStartWorkSession(path, text)
    },
    [actionIsCurrent, scope.attachments]
  )

  // Branch off into a NEW worktree (base = branch name, or current HEAD). A
  // create failure throws back to the row (which toasts) before we touch the
  // draft; a missing cwd / remote backend no-ops (the row hides the affordance).
  const handleBranchOff = useCallback(
    async (branch: string, base?: string) => {
      if (!actionIsCurrent()) {
        return
      }

      const repoPath = actionStateRef.current.cwd?.trim()
      const result = repoPath && (await startWorkInRepo(repoPath, { base, branch, name: branch }))

      if (result) {
        openInWorktree(result.path)
      }
    },
    [actionIsCurrent, openInWorktree]
  )

  // Convert an EXISTING branch into a fresh worktree + session (no new branch).
  // Mirrors handleBranchOff's hand-off: create the worktree, then open a session
  // anchored there carrying the draft.
  const handleConvertBranch = useCallback(
    async (branch: string, path?: null | string, isDefault?: boolean) => {
      if (!actionIsCurrent()) {
        return
      }

      if (path?.trim()) {
        openInWorktree(path)

        return
      }

      const repoPath = actionStateRef.current.cwd?.trim()

      if (repoPath && isDefault) {
        await switchBranchInRepo(repoPath, branch)
        openInWorktree(repoPath)

        return
      }

      const result = repoPath && (await startWorkInRepo(repoPath, { existingBranch: branch }))

      if (result) {
        openInWorktree(result.path)
      }
    },
    [actionIsCurrent, openInWorktree]
  )

  const handleListBranches = useCallback(async () => {
    if (!actionIsCurrent()) {
      return []
    }

    const repoPath = actionStateRef.current.cwd?.trim()

    return repoPath ? listRepoBranches(repoPath) : []
  }, [actionIsCurrent])

  const handleSwitchBranch = useCallback(
    async (branch: string) => {
      if (!actionIsCurrent()) {
        return
      }

      const repoPath = actionStateRef.current.cwd?.trim()

      if (repoPath) {
        await switchBranchInRepo(repoPath, branch)
      }
    },
    [actionIsCurrent]
  )

  return { handleBranchOff, handleConvertBranch, handleListBranches, handleSwitchBranch, openInWorktree }
}
