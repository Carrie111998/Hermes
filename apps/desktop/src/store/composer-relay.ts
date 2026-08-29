/**
 * COMPOSER RELAY — attachments made in a window that has no composer.
 *
 * Every desktop window is its own renderer with its own composer store, and a
 * popped-out Browser window has no composer at all. So "Attach to chat" from a
 * popped-out preview called `addComposerAttachment` on a store nothing renders:
 * the click succeeded, the chip landed in an atom no one was watching, and the
 * user saw nothing happen.
 *
 * Same shape as `session-sync.ts`: a BroadcastChannel, never delivered to its
 * own poster, with the primary window as the only listener.
 */

import type { ComposerAttachment } from './composer'

const CHANNEL = 'hermes:composer-attachment'

const channel = typeof BroadcastChannel === 'undefined' ? null : new BroadcastChannel(CHANNEL)

/**
 * Hand an attachment to whichever window owns the composer.
 *
 * Returns false when there is no channel to hand it to, so the caller can say
 * so rather than claim a success it cannot see.
 */
export function relayComposerAttachment(attachment: ComposerAttachment): boolean {
  if (!channel) {return false}

  try {
    // Structured-cloned, so only plain data crosses. Everything a
    // ComposerAttachment carries already is.
    channel.postMessage(attachment)

    return true
  } catch {
    return false
  }
}

export function onRelayedComposerAttachment(handler: (attachment: ComposerAttachment) => void): () => void {
  if (!channel) {
    return () => {}
  }

  const listener = (event: MessageEvent) => {
    const attachment = event.data as ComposerAttachment | undefined

    if (attachment && typeof attachment.id === 'string' && typeof attachment.kind === 'string') {
      handler(attachment)
    }
  }

  channel.addEventListener('message', listener)

  return () => channel.removeEventListener('message', listener)
}
