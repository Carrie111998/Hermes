import { atom } from 'nanostores'

// Config-gated tools (browser, vision, tts, ...) that failed their check_fn
// for this session (issue #1433 Phase 2). The gateway emits `tools.unavailable`
// once per session build; the thread renders the notice in the fresh-session
// empty state. Keyed by session id; a stale entry is inert because it only
// renders while that session's thread is mounted.
const $toolsUnavailable = atom<Record<string, string[]>>({})

export { $toolsUnavailable }

export function setToolsUnavailable(sessionId: string, names: string[]) {
  if (!sessionId) {
    return
  }

  const current = $toolsUnavailable.get()

  $toolsUnavailable.set({ ...current, [sessionId]: names })
}
