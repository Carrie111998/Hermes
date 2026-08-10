import { requestComposerSubmit } from '@/app/chat/composer/focus'

/**
 * Start the primary session's existing `/compress` command through the composer
 * command bus. Keeping this as a composer submit (instead of calling the raw
 * gateway RPC) preserves Desktop's transcript replacement, session-id recovery,
 * usage refresh, long timeout, and authoritative compaction lifecycle handling.
 */
export function requestPrimarySessionCompression(): void {
  requestComposerSubmit('/compress', { preserveDraft: true, target: 'main' })
}
