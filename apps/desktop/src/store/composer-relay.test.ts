import { describe, expect, it, vi } from 'vitest'

import type { ComposerAttachment } from './composer'
import { onRelayedComposerAttachment, relayComposerAttachment } from './composer-relay'

const chip: ComposerAttachment = {
  detail: '[]',
  id: 'pins:a,b',
  kind: 'pins',
  label: '2 comments'
}

/** The other window. `relayComposerAttachment` and `onRelayedComposerAttachment`
 *  share one module-level channel, and a BroadcastChannel never delivers to its
 *  own poster — so a second channel is the only way to play the far side. */
const otherWindow = () => new BroadcastChannel('hermes:composer-attachment')

describe('composer relay', () => {
  it('says the hand-off failed when no window took delivery', async () => {
    if (typeof BroadcastChannel === 'undefined') {return}

    // The bug this exists to stop: posting into an empty room succeeds just as
    // happily as posting into a full one, so "we posted it" is not an answer.
    // Nobody is listening here, so the honest result is false.
    await expect(relayComposerAttachment(chip)).resolves.toBe(false)
  })

  it('says it succeeded once the composer window acknowledges', async () => {
    if (typeof BroadcastChannel === 'undefined') {return}

    const other = otherWindow()
    other.addEventListener('message', event => {
      const data = event.data as { requestId?: string }

      if (typeof data?.requestId === 'string') {other.postMessage({ ack: data.requestId })}
    })

    await expect(relayComposerAttachment(chip)).resolves.toBe(true)
    other.close()
  })

  it('carries an attachment to a listener in another window', async () => {
    if (typeof BroadcastChannel === 'undefined') {return}

    const received: ComposerAttachment[] = []
    const stop = onRelayedComposerAttachment(attachment => received.push(attachment))
    const other = otherWindow()
    other.postMessage({ attachment: chip, requestId: 'r1' })

    await vi.waitFor(() => expect(received).toHaveLength(1))
    expect(received[0].kind).toBe('pins')
    expect(received[0].label).toBe('2 comments')

    other.close()
    stop()
  })

  it('acknowledges what it took, by the sender\'s own id', async () => {
    if (typeof BroadcastChannel === 'undefined') {return}

    const acks: unknown[] = []
    const stop = onRelayedComposerAttachment(() => {})
    const other = otherWindow()
    other.addEventListener('message', event => {
      const data = event.data as { ack?: string }

      if (typeof data?.ack === 'string') {acks.push(data.ack)}
    })
    other.postMessage({ attachment: chip, requestId: 'r2' })

    await vi.waitFor(() => expect(acks).toEqual(['r2']))

    other.close()
    stop()
  })

  it('ignores anything that is not a relayed attachment', async () => {
    if (typeof BroadcastChannel === 'undefined') {return}

    const received: unknown[] = []
    const stop = onRelayedComposerAttachment(attachment => received.push(attachment))
    const other = otherWindow()
    other.postMessage(null)
    other.postMessage('nope')
    other.postMessage({ id: 7 })
    // A bare attachment with no envelope: the pre-ack wire format, which must
    // not be mistaken for a request nobody can acknowledge.
    other.postMessage(chip)
    other.postMessage({ attachment: chip, requestId: 'r3' })

    await vi.waitFor(() => expect(received).toHaveLength(1))

    other.close()
    stop()
  })

  it('unsubscribes', async () => {
    if (typeof BroadcastChannel === 'undefined') {return}

    const received: unknown[] = []
    onRelayedComposerAttachment(attachment => received.push(attachment))()
    const other = otherWindow()
    other.postMessage({ attachment: chip, requestId: 'r4' })
    await new Promise(resolve => setTimeout(resolve, 30))
    expect(received).toHaveLength(0)
    other.close()
  })
})
