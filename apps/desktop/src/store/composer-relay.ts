/**
 * COMPOSER RELAY — attachments made in a window that has no composer.
 *
 * Every desktop window is its own renderer with its own composer store, so
 * `addComposerAttachment` only ever fills the composer of the window that
 * called it. That is right for every window EXCEPT the popped-out Browser,
 * which has no composer at all: there the click succeeded, the chip landed in
 * an atom nothing renders, and the user saw nothing happen.
 *
 * Only that one window relays. `isAuxiliaryWindow()` looks like the right test
 * and is not — it also covers the secondary session window and the HUD, and
 * both of those are full app renderers with a real composer of their own
 * (see the comments in `store/windows.ts`). Relaying from one of those posts
 * the chip to the PRIMARY window, which is not the window the user is looking
 * at: the same invisible success, one layer further away.
 *
 * Same shape as `session-sync.ts`: a BroadcastChannel, never delivered to its
 * own poster, with the primary window as the only listener. The one addition is
 * an acknowledgement — see `relayComposerAttachment`.
 */

import type { ComposerAttachment } from './composer'

const CHANNEL = 'hermes:composer-attachment'

/** How long to wait for the composer's window to say it took delivery. This is
 *  a same-machine post between two renderers of one Electron app; it either
 *  lands in a frame or there is nobody there. Generous on purpose — the cost of
 *  waiting is a slightly later toast, the cost of being early is a false "could
 *  not add". */
const ACK_TIMEOUT_MS = 400

interface RelayMessage {
  attachment: ComposerAttachment
  requestId: string
}

interface AckMessage {
  ack: string
}

const channel = typeof BroadcastChannel === 'undefined' ? null : new BroadcastChannel(CHANNEL)

let counter = 0

const isRelay = (data: unknown): data is RelayMessage => {
  const message = data as null | Partial<RelayMessage>
  const attachment = message?.attachment

  return (
    typeof message?.requestId === 'string' &&
    Boolean(attachment) &&
    typeof attachment?.id === 'string' &&
    typeof attachment.kind === 'string'
  )
}

const isAck = (data: unknown): data is AckMessage => typeof (data as null | Partial<AckMessage>)?.ack === 'string'

/**
 * Hand an attachment to whichever window owns the composer, and report whether
 * that window actually took it.
 *
 * Posting cannot fail usefully — `postMessage` succeeds into an empty room just
 * as happily as a full one — so a bare "we posted it" is exactly the false
 * success this whole file exists to stop. The receiving window acknowledges by
 * id; no acknowledgement inside {@link ACK_TIMEOUT_MS} means nobody took it,
 * and the caller is free to say so.
 */
export function relayComposerAttachment(attachment: ComposerAttachment): Promise<boolean> {
  if (!channel) {return Promise.resolve(false)}

  counter += 1
  const requestId = `relay-${counter}-${attachment.id}`

  return new Promise<boolean>(resolve => {
    let settled = false

    const finish = (delivered: boolean) => {
      if (settled) {return}

      settled = true
      channel.removeEventListener('message', onMessage)
      clearTimeout(timer)
      resolve(delivered)
    }

    const onMessage = (event: MessageEvent) => {
      if (isAck(event.data) && event.data.ack === requestId) {finish(true)}
    }

    const timer = setTimeout(() => finish(false), ACK_TIMEOUT_MS)

    channel.addEventListener('message', onMessage)

    try {
      // Structured-cloned, so only plain data crosses. Everything a
      // ComposerAttachment carries already is.
      channel.postMessage({ attachment, requestId } satisfies RelayMessage)
    } catch {
      finish(false)
    }
  })
}

/** Subscribe the composer-owning window. Acknowledges every attachment it takes
 *  so the sender can tell delivery from a post into an empty room. */
export function onRelayedComposerAttachment(handler: (attachment: ComposerAttachment) => void): () => void {
  if (!channel) {
    return () => {}
  }

  const listener = (event: MessageEvent) => {
    if (!isRelay(event.data)) {return}

    handler(event.data.attachment)

    try {
      channel.postMessage({ ack: event.data.requestId } satisfies AckMessage)
    } catch {
      // The chip is already in the composer; a lost acknowledgement costs the
      // sender a wrong toast, not the user's attachment.
    }
  }

  channel.addEventListener('message', listener)

  return () => channel.removeEventListener('message', listener)
}
