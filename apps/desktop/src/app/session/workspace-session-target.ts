import type { MutableRefObject } from 'react'

import type { NewChatOwner } from '@/store/profile'
import { followActiveSessionCwd, resolveNewSessionCwd, type StartWorkSessionRequest } from '@/store/projects'
import {
  $newChatWorkspaceTargetGeneration,
  type NewChatWorkspaceTarget,
  setCurrentBranch,
  setCurrentCwd,
  setNewChatWorkspaceTarget
} from '@/store/session'

interface WorkspaceSessionOptions {
  activeSessionIdRef: MutableRefObject<string | null>
  followActiveSessionCwd?: (cwd: string) => void | Promise<void>
  onExplicitWorkspace?: (cwd: string) => void
  owner?: NewChatOwner
  path: null | string
  requestGateway: <T>(method: string, params?: Record<string, unknown>) => Promise<T>
  startFreshSessionDraft: (options?: { owner?: NewChatOwner; workspaceTarget?: NewChatWorkspaceTarget }) => void
}

export function handleStartWorkSessionRequest(
  request: StartWorkSessionRequest,
  startSessionInWorkspace: (path: string, options: { openTab?: boolean; owner?: NewChatOwner }) => void,
  insertDraft: (draft: string) => void
): void {
  startSessionInWorkspace(request.path, {
    openTab: request.openTab,
    owner: request.owner
  })

  if (request.draft) {
    insertDraft(request.draft)
  }
}

export function startWorkspaceSession({
  activeSessionIdRef,
  followActiveSessionCwd: followCwd = followActiveSessionCwd,
  onExplicitWorkspace,
  owner,
  path,
  requestGateway,
  startFreshSessionDraft
}: WorkspaceSessionOptions): void {
  // Home's "+" passes path=null on purpose ("no folder"). That must stay
  // detached — do NOT fall through to resolveNewSessionCwd(), which can still
  // return a default/remembered project folder and re-attach the last repo
  // (digitwo: New session in Home still shows `main`).
  if (path === null) {
    startFreshSessionDraft({ ...(owner !== undefined ? { owner } : {}), workspaceTarget: null })

    return
  }

  // A worktree lane carries its own path. Empty string (legacy/path-less trunk)
  // can fall back to the active project's root, but null was handled above.
  const explicitTarget = path.trim()
  const target = explicitTarget || resolveNewSessionCwd()

  startFreshSessionDraft(
    target
      ? { ...(owner !== undefined ? { owner } : {}), workspaceTarget: target }
      : owner !== undefined
        ? { owner }
        : undefined
  )

  if (!target) {
    return
  }

  const workspaceGeneration = $newChatWorkspaceTargetGeneration.get()

  setCurrentCwd(target)
  void requestGateway<{ branch?: string; cwd?: string }>('config.get', { key: 'project', cwd: target })
    .then(info => {
      if ($newChatWorkspaceTargetGeneration.get() !== workspaceGeneration || activeSessionIdRef.current) {
        return
      }

      const resolved = info.cwd || target

      setCurrentCwd(resolved)
      setNewChatWorkspaceTarget(resolved)
      setCurrentBranch(info.branch || '')

      if (explicitTarget) {
        onExplicitWorkspace?.(resolved)
        void followCwd(resolved)
      }
    })
    .catch(() => {
      if ($newChatWorkspaceTargetGeneration.get() === workspaceGeneration && !activeSessionIdRef.current) {
        setCurrentBranch('')
      }
    })
}
