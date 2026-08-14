import { requestComposerSubmit } from '@/app/chat/composer/focus'

export const DEFAULT_KEEP_RECENT_TURNS = 2
export const KEEP_RECENT_TURN_OPTIONS = Array.from({ length: 10 }, (_, index) => index + 1)
export type KeepRecentTurns = number | null

/** Build the established boundary-aware manual compression command. */
export function primarySessionCompressionCommand(
  keepRecentTurns: KeepRecentTurns = DEFAULT_KEEP_RECENT_TURNS
): string {
  if (keepRecentTurns === null) {
    return '/compress'
  }

  const normalizedTurns = Number.isFinite(keepRecentTurns) ? Math.trunc(keepRecentTurns) : DEFAULT_KEEP_RECENT_TURNS
  const boundedTurns = Math.max(1, Math.min(100, normalizedTurns))

  return `/compress here ${boundedTurns}`
}

/**
 * Start the primary session's existing `/compress` command through the composer
 * command bus. Keeping this as a composer submit (instead of calling the raw
 * gateway RPC) preserves Desktop's transcript replacement, session-id recovery,
 * usage refresh, long timeout, authoritative lifecycle handling, and the
 * current draft. `/compress here N` additionally preserves the latest N
 * complete user/assistant exchanges verbatim.
 */
export function requestPrimarySessionCompression(
  keepRecentTurns: KeepRecentTurns = DEFAULT_KEEP_RECENT_TURNS
): void {
  requestComposerSubmit(primarySessionCompressionCommand(keepRecentTurns), {
    preserveDraft: true,
    target: 'main'
  })
}
