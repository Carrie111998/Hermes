import { atom } from 'nanostores'

export type ProfileConversationRestoreOrigin = 'connection-switch' | 'profile-switch'

export interface ProfileConversationRestoreTarget {
  connectionId: null | string
  profile: string
}

interface ProfileConversationRestoreBase {
  origin: ProfileConversationRestoreOrigin
  sequence: number
  target: ProfileConversationRestoreTarget
}

export type ProfileConversationRestoreRequest =
  | (ProfileConversationRestoreBase & { phase: 'activating' })
  | (ProfileConversationRestoreBase & { phase: 'committed' })
  | (ProfileConversationRestoreBase & { phase: 'navigating'; sessionId: string })

export type AutomaticFreshDraftCause =
  | 'boot-transition'
  | 'connection-switch'
  | 'context-recovery'
  | 'gateway-transition'
  | 'profile-switch'
  | 'switch-recovery'

export type ExplicitFreshDraftCause =
  | 'close-chat'
  | 'message-on-automatic-draft'
  | 'new-chat'
  | 'new-chat-in-agent'
  | 'new-chat-in-profile'
  | 'new-project-chat'

export type FreshSessionIntentInput =
  | {
      cause: AutomaticFreshDraftCause
      persistence: 'automatic'
      restoreSequence?: number
    }
  | {
      cause: ExplicitFreshDraftCause
      persistence: 'explicit'
    }

export type FreshSessionIntent = FreshSessionIntentInput & { sequence: number }

export type AppliedFreshDraftProvenance =
  | {
      cause: AutomaticFreshDraftCause
      freshSequence: number
      kind: 'automatic'
      restoreSequence?: number
    }
  | {
      cause: ExplicitFreshDraftCause
      freshSequence: number
      kind: 'explicit'
    }

export const $profileConversationRestore = atom<null | ProfileConversationRestoreRequest>(null)
export const $appliedFreshDraftProvenance = atom<AppliedFreshDraftProvenance | null>(null)

let restoreSequence = 0
let freshSequence = 0

/** Create one classified reset intent for direct and counter-driven producers. */
export function createFreshSessionIntent(input: FreshSessionIntentInput): FreshSessionIntent {
  return { ...input, sequence: ++freshSequence }
}

export function provenanceForFreshSessionIntent(intent: FreshSessionIntent): AppliedFreshDraftProvenance {
  return intent.persistence === 'automatic'
    ? {
        cause: intent.cause,
        freshSequence: intent.sequence,
        kind: 'automatic',
        ...(intent.restoreSequence === undefined ? {} : { restoreSequence: intent.restoreSequence })
      }
    : { cause: intent.cause, freshSequence: intent.sequence, kind: 'explicit' }
}

function normalizeTarget(target: ProfileConversationRestoreTarget): ProfileConversationRestoreTarget {
  const connectionId = target.connectionId?.trim() || null
  const profile = target.profile.trim() || 'default'

  return { connectionId, profile }
}

/** Begin a latest-only live restore transaction, superseding any older request. */
export function beginProfileConversationRestore(
  origin: ProfileConversationRestoreOrigin,
  target: ProfileConversationRestoreTarget
): number {
  const sequence = ++restoreSequence

  $profileConversationRestore.set({
    origin,
    phase: 'activating',
    sequence,
    target: normalizeTarget(target)
  })

  return sequence
}

/** Commit only the latest matching activation. */
export function commitProfileConversationRestore(sequence: number): boolean {
  const current = $profileConversationRestore.get()

  if (!current || current.sequence !== sequence || current.phase !== 'activating') {
    return false
  }

  $profileConversationRestore.set({ ...current, phase: 'committed' })

  return true
}

/** Claim the matching route navigation before publishing it to React Router. */
export function markProfileConversationRestoreNavigating(sequence: number, sessionId: string): boolean {
  const current = $profileConversationRestore.get()
  const durableId = sessionId.trim()

  if (!current || current.sequence !== sequence || current.phase !== 'committed' || !durableId) {
    return false
  }

  $profileConversationRestore.set({ ...current, phase: 'navigating', sessionId: durableId })

  return true
}

/** Complete only the latest matching restore. */
export function completeProfileConversationRestore(sequence: number): void {
  if (isCurrentProfileConversationRestore(sequence)) {
    $profileConversationRestore.set(null)
  }
}

/**
 * Cancel one matching generation, or the current generation when no sequence is
 * supplied. The reason is accepted for call-site diagnostics without becoming
 * renderer state.
 */
export function cancelProfileConversationRestore(sequence?: number, _reason?: string): void {
  const current = $profileConversationRestore.get()

  if (current && (sequence === undefined || current.sequence === sequence)) {
    $profileConversationRestore.set(null)
  }
}

export function isCurrentProfileConversationRestore(sequence: number): boolean {
  return $profileConversationRestore.get()?.sequence === sequence
}

/** Record the reset that currently owns the visible fresh draft. */
export function applyFreshDraftProvenance(provenance: AppliedFreshDraftProvenance): void {
  $appliedFreshDraftProvenance.set(provenance)
}

export function clearAppliedFreshDraftProvenance(): void {
  $appliedFreshDraftProvenance.set(null)
}

/**
 * Adopt an automatically-created isolation draft as the user's explicit blank
 * before a prompt/create attempt. This is deliberately synchronous: callers
 * invoke it at the submission boundary, before any async session creation can
 * race persistence or a pending restore completion.
 */
export function promoteAutomaticFreshDraftToExplicit(): void {
  const current = $appliedFreshDraftProvenance.get()

  cancelProfileConversationRestore(undefined, 'message-on-automatic-draft')

  if (current?.kind !== 'automatic') {
    return
  }

  applyFreshDraftProvenance(
    provenanceForFreshSessionIntent(
      createFreshSessionIntent({ cause: 'message-on-automatic-draft', persistence: 'explicit' })
    )
  )
}

/** @internal */
export function _resetProfileConversationRestoreForTests(): void {
  restoreSequence = 0
  freshSequence = 0
  $profileConversationRestore.set(null)
  $appliedFreshDraftProvenance.set(null)
}
