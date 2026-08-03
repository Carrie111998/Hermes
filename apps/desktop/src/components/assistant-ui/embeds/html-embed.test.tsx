import { cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { INLINE_HEIGHT_MESSAGE, InlineHtmlEmbed, MAX_HEIGHT } from './html-embed'

const MIN_HEIGHT = 60

const FRAG = '<h1>Pomodoro</h1><p>tiny timer</p>'

function postHeight(frame: HTMLIFrameElement, height: number) {
  // Simulate the sandboxed document's height-sync shim: a message that
  // originates from the iframe's own contentWindow (the same origin check the
  // component enforces).
  window.dispatchEvent(
    new MessageEvent('message', {
      data: { [INLINE_HEIGHT_MESSAGE]: height },
      source: frame.contentWindow,
    })
  )
}

describe('InlineHtmlEmbed height clamp', () => {
  afterEach(() => cleanup())

  it('sizes the frame to the reported height within the allowed range', async () => {
    const { container } = render(<InlineHtmlEmbed code={FRAG} streaming={false} />)
    const frame = container.querySelector('iframe') as HTMLIFrameElement

    postHeight(frame, 480)
    await waitFor(() => expect(frame.style.height).toBe('480px'))
  })

  it('clamps an oversized reported height to MAX_HEIGHT', async () => {
    const { container } = render(<InlineHtmlEmbed code={FRAG} streaming={false} />)
    const frame = container.querySelector('iframe') as HTMLIFrameElement

    postHeight(frame, Number.MAX_SAFE_INTEGER)
    await waitFor(() => expect(frame.style.height).toBe(`${MAX_HEIGHT}px`))

    postHeight(frame, MAX_HEIGHT + 5000)
    await waitFor(() => expect(frame.style.height).toBe(`${MAX_HEIGHT}px`))
  })

  it('floors a degenerate small height to MIN_HEIGHT', async () => {
    const { container } = render(<InlineHtmlEmbed code={FRAG} streaming={false} />)
    const frame = container.querySelector('iframe') as HTMLIFrameElement

    postHeight(frame, 0)
    await waitFor(() => expect(frame.style.height).toBe(`${MIN_HEIGHT}px`))
  })

  it('ignores messages that do not originate from its own frame', () => {
    const { container } = render(<InlineHtmlEmbed code={FRAG} streaming={false} />)
    const frame = container.querySelector('iframe') as HTMLIFrameElement

    const before = frame.style.height

    // A foreign window (e.g. another embed or the app shell) must not be able
    // to resize this frame.
    window.dispatchEvent(
      new MessageEvent('message', {
        data: { [INLINE_HEIGHT_MESSAGE]: MAX_HEIGHT },
        source: window,
      })
    )
    expect(frame.style.height).toBe(before)
  })

  it('starts at the default height and mounts after streaming settles', () => {
    const { container, rerender } = render(<InlineHtmlEmbed code={FRAG} streaming={true} />)

    // While streaming: shimmer placeholder, no iframe yet.
    expect(container.querySelector('[data-slot="inline-html-placeholder"]')).not.toBeNull()
    expect(container.querySelector('iframe')).toBeNull()

    // Once the fence settles: placeholder is replaced by the live iframe.
    rerender(<InlineHtmlEmbed code={FRAG} streaming={false} />)
    const frame = container.querySelector('iframe') as HTMLIFrameElement
    expect(container.querySelector('[data-slot="inline-html-placeholder"]')).toBeNull()
    expect(frame).not.toBeNull()
    expect(frame.getAttribute('srcdoc')).toContain('<h1>Pomodoro</h1>')

    // The default start height applies until the frame reports its content.
    expect(frame.style.height).toBe('340px')
  })
})
