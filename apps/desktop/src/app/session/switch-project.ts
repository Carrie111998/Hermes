import type { MutableRefObject } from 'react'

import { normalizeProjectPath } from '@/lib/project-paths'
import { probeProjectExists } from '@/store/project-switcher'
import { markProjectMissing, recordRecentProject } from '@/store/recent-projects'

export interface SwitchToProjectOptions {
  /** Live pointer to the focused session — read AFTER every await. */
  activeSessionIdRef: MutableRefObject<string | null>
  /** The existing cwd mutation (per-session or new-chat target). */
  changeSessionCwd: (cwd: string) => Promise<void>
  path: string
  /** Injected for tests; defaults to the real fs probe. */
  probeExists?: (path: string) => Promise<boolean>
}

export type SwitchProjectResult = 'missing' | 'invalid' | 'session-changed' | 'switched'

/**
 * Point the workspace at a recent project.
 *
 * Two invariants this function exists to hold:
 *
 * 1. **Never re-anchor a dead path.** A remembered folder can be deleted,
 *    renamed, or unmounted between launches. We probe first and mark it missing
 *    instead of handing the agent's terminal/file tools a path that isn't there.
 *
 * 2. **Never re-anchor the WRONG session.** `use-cwd-actions` documents this the
 *    hard way: re-anchoring the wrong session's workspace points that agent's
 *    tools at another conversation's project. The existence probe adds an await
 *    BEFORE the mutation, so the user can switch chats mid-probe. We therefore
 *    latch the session that was focused when the switch was requested and abort
 *    if focus moved while we were awaiting — rather than letting a stale intent
 *    land on whichever conversation happens to be open when the probe resolves.
 *    `changeSessionCwd` then re-reads the same ref to do the actual write, so
 *    the ref stays the single source of truth for "which session am I acting on".
 */
export async function switchToProject({
  activeSessionIdRef,
  changeSessionCwd,
  path,
  probeExists = probeProjectExists
}: SwitchToProjectOptions): Promise<SwitchProjectResult> {
  const target = normalizeProjectPath(path)

  if (!target) {
    return 'invalid'
  }

  const requestedForSession = activeSessionIdRef.current
  const exists = await probeExists(target)

  if (!exists) {
    markProjectMissing(target, true)

    return 'missing'
  }

  // Focus moved while the probe was in flight — drop the stale intent.
  if (activeSessionIdRef.current !== requestedForSession) {
    return 'session-changed'
  }

  await changeSessionCwd(target)
  // Record only AFTER a successful switch, so the MRU reflects workspaces the
  // user actually opened rather than every path they hovered.
  recordRecentProject(target)

  return 'switched'
}
