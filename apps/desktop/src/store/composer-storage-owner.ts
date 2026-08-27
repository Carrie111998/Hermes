import type { PrimarySessionOwnerIntent } from '@/store/session'
import type { SessionOwnerRoute, SessionOwnerScope } from '@/store/session-request-router'

import { type ComposerStorageOwner, normalizeComposerStorageOwner } from './composer-storage-scope'

interface ResolveComposerStorageOwnerOptions {
  ambientOwner: ComposerStorageOwner
  isPrimary: boolean
  knownOwner?: SessionOwnerScope
  newChatOwner?: SessionOwnerRoute | null
  primaryIntent?: PrimarySessionOwnerIntent | null
  selectedSessionId: string | null
  tileOwner?: SessionOwnerRoute
}

export function composerOwnerMatchesUniqueHint(
  owner: ComposerStorageOwner,
  hint: SessionOwnerRoute | undefined
): boolean {
  if (!hint) {
    return false
  }

  const normalizedOwner = normalizeComposerStorageOwner(owner)

  const normalizedHint = normalizeComposerStorageOwner({
    connectionId: hint.connectionId,
    profile: hint.profile
  })

  return (
    normalizedOwner.connectionId === normalizedHint.connectionId && normalizedOwner.profile === normalizedHint.profile
  )
}

/** Select the exact renderer storage owner without treating ambient chrome as
 * authority for a routed session. Exact surface intent outranks row/hint
 * inference; New Chat follows the owner route its create RPC will use. */
export function resolveComposerStorageOwner({
  ambientOwner,
  isPrimary,
  knownOwner,
  newChatOwner,
  primaryIntent,
  selectedSessionId,
  tileOwner
}: ResolveComposerStorageOwnerOptions): ComposerStorageOwner {
  if (!isPrimary && tileOwner) {
    return { connectionId: tileOwner.connectionId, profile: tileOwner.profile }
  }

  if (isPrimary && selectedSessionId === null && newChatOwner) {
    return { connectionId: newChatOwner.connectionId, profile: newChatOwner.profile }
  }

  if (isPrimary && primaryIntent?.storedSessionId === selectedSessionId) {
    return {
      connectionId: primaryIntent.ownerRoute.connectionId,
      profile: primaryIntent.ownerRoute.profile
    }
  }

  if (knownOwner && typeof knownOwner === 'object') {
    return { connectionId: knownOwner.connectionId, profile: knownOwner.profile }
  }

  if (typeof knownOwner === 'string') {
    return { connectionId: ambientOwner.connectionId, profile: knownOwner }
  }

  return ambientOwner
}
