import { requestStartWorkSession } from '@/store/projects'
import { $focusedStoredSessionId, knownOwnerForSession } from '@/store/session-states'

/** Capture the focused surface owner at palette selection time. */
export function startFocusedWorktreeSession(path: string): void {
  const owner = knownOwnerForSession($focusedStoredSessionId.get())

  requestStartWorkSession(path, undefined, owner ? { owner } : undefined)
}
