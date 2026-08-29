import { describe, expect, it, vi } from 'vitest'

import type { ComposerAttachment } from './composer'
import { onRelayedComposerAttachment, relayComposerAttachment } from './composer-relay'

const chip: ComposerAttachment = {
  detail: '[]',
  id: 'pins:a,b',
  kind: 'pins',
  label: '2 comments'
}

describe('composer relay', () => {
  it('reports whether the hand-off could even be attempted', () => {
    // The caller uses this to tell the user "added" or "no composer window",
    // instead of a click that looks inert either way.
    expect(typeof relayComposerAttachment(chip)).toBe('boolean')
  })

  it('carries an attachment to a listener in another window', async () => {
    if (typeof BroadcastChannel === 'undefined') {return}

    // A BroadcastChannel never delivers to its own poster, so stand in for the
    // other window with a second channel on the same name.
    const received: ComposerAttachment[] = []
    const stop = onRelayedComposerAttachment(attachment => received.push(attachment))
    const other = new BroadcastChannel('hermes:composer-attachment')
    other.postMessage(chip)

    await vi.waitFor(() => expect(received).toHaveLength(1))
    expect(received[0].kind).toBe('pins')
    expect(received[0].label).toBe('2 comments')

    other.close()
    stop()
  })

  it('ignores anything that is not an attachment', async () => {
    if (typeof BroadcastChannel === 'undefined') {return}

    const received: unknown[] = []
    const stop = onRelayedComposerAttachment(attachment => received.push(attachment))
    const other = new BroadcastChannel('hermes:composer-attachment')
    other.postMessage(null)
    other.postMessage('nope')
    other.postMessage({ id: 7 })
    other.postMessage(chip)

    await vi.waitFor(() => expect(received).toHaveLength(1))

    other.close()
    stop()
  })

  it('unsubscribes', async () => {
    if (typeof BroadcastChannel === 'undefined') {return}

    const received: unknown[] = []
    onRelayedComposerAttachment(attachment => received.push(attachment))()
    const other = new BroadcastChannel('hermes:composer-attachment')
    other.postMessage(chip)
    await new Promise(resolve => setTimeout(resolve, 30))
    expect(received).toHaveLength(0)
    other.close()
  })
})
