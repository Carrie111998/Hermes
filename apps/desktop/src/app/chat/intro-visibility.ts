import type {
  AppliedFreshDraftProvenance,
  ProfileConversationRestoreRequest
} from '@/store/profile-conversation-restore'

/**
 * Whether an in-flight remembered-conversation restore should replace the
 * current transcript with the loading surface.
 *
 * Profile switches mutate the active backend profile in place, so the old
 * transcript stops being authoritative as soon as activation begins.
 * Connection switches are deliberately two-phase: their pre-dial keeps the
 * current source fully usable. Do not flash a loader until the commit has
 * applied the matching automatic isolation draft; unrelated or stale draft
 * provenance must never blank the visible conversation.
 */
export function shouldShowConversationRestoreLoading(input: {
  primary: boolean
  provenance: AppliedFreshDraftProvenance | null
  restore: ProfileConversationRestoreRequest | null
}): boolean {
  if (!input.primary || !input.restore) {
    return false
  }

  if (input.restore.origin === 'profile-switch') {
    return true
  }

  return (
    input.restore.phase !== 'activating' &&
    input.provenance?.kind === 'automatic' &&
    input.provenance.cause === 'connection-switch' &&
    input.provenance.restoreSequence === input.restore.sequence
  )
}

/**
 * Whether the empty-chat intro splash renders.
 *
 * The splash is the full-height empty state of the primary chat: it belongs to
 * a fresh draft in the main window and nothing else. Auxiliary and non-primary
 * windows are scratch surfaces, a routed or active session already owns the
 * view, and any transcript at all means the conversation started.
 *
 * `enabled` is the user's Appearance toggle and outranks every other clause:
 * turning the splash off never depends on which window asks.
 */
export function shouldShowIntro(input: {
  activeSessionId: null | string
  auxiliaryWindow: boolean
  enabled: boolean
  freshDraftReady: boolean
  messagesEmpty: boolean
  primary: boolean
  restoringConversation: boolean
  routedSessionView: boolean
  selectedSessionId: null | string
}): boolean {
  return (
    input.enabled &&
    input.primary &&
    !input.auxiliaryWindow &&
    !input.restoringConversation &&
    input.freshDraftReady &&
    !input.routedSessionView &&
    !input.selectedSessionId &&
    !input.activeSessionId &&
    input.messagesEmpty
  )
}
