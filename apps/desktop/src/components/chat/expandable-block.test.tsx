import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ExpandableBlock } from './expandable-block'

// jsdom has no ResizeObserver and reports scrollHeight === 0, so the block
// never flips to `overflowing` on its own. Stub RO to fire immediately and
// force a tall scrollHeight on the observed node so the toggle mounts.
class TestResizeObserver {
  constructor(private readonly callback: ResizeObserverCallback) {}

  observe(target: Element) {
    Object.defineProperty(target, 'scrollHeight', { configurable: true, value: 400 })
    Object.defineProperty(target, 'offsetWidth', { configurable: true, value: 200 })
    Object.defineProperty(target, 'clientWidth', { configurable: true, value: 185 })

    this.callback([{ target } as ResizeObserverEntry], this as unknown as ResizeObserver)
  }

  unobserve() {}
  disconnect() {}
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('ExpandableBlock', () => {
  it('keeps scroll interactions free and offsets controls by the vertical gutter', () => {
    vi.stubGlobal('ResizeObserver', TestResizeObserver)
    const onScrollbarSizeChange = vi.fn()

    const { container } = render(
      <ExpandableBlock onScrollbarSizeChange={onScrollbarSizeChange}>
        <pre data-testid="content">{'const x = 1\n'.repeat(20)}</pre>
      </ExpandableBlock>
    )

    const inner = container.querySelector('[data-testid="content"]')!.parentElement!
    const toggle = screen.getByRole('button', { name: /expand|collapse/i })
    const fade = toggle.parentElement!

    // Preserve the native overlay treatment. On platforms where Chromium uses
    // a classic gutter instead, measurement—not restyling—moves the controls.
    expect(inner.className).toContain('overflow-x-auto')
    expect(inner.className).toContain('scrollbar-overlay')

    // The full-width fade is a pure cue: it spans the bottom edge but must not
    // intercept pointer events, so the scrollbar drag and text selection on the
    // last line pass through to the content underneath.
    expect(fade.className).toContain('pointer-events-none')
    expect(fade.className).toContain('inset-x-0')

    // Only the compact toggle is clickable, and it is pinned to the right edge
    // rather than spanning the full width (the old bug). Its measured margins
    // match the native scrollbar gutters, so overlay bars stay unchanged while
    // classic Linux/Windows bars get their exact hit area back.
    expect(toggle.className).toContain('pointer-events-auto')
    expect(toggle.className).toContain('w-9')
    expect(toggle.style.marginRight).toBe('15px')
    expect(onScrollbarSizeChange).toHaveBeenLastCalledWith({ inline: 15 })
    expect(toggle.className).not.toContain('inset-x-0')
  })

  it('still toggles expanded state when the compact control is clicked', () => {
    vi.stubGlobal('ResizeObserver', TestResizeObserver)

    render(
      <ExpandableBlock>
        <pre data-testid="content">{'line\n'.repeat(20)}</pre>
      </ExpandableBlock>
    )

    const toggle = screen.getByRole('button', { name: 'Expand' })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')

    fireEvent.click(toggle)

    expect(screen.getByRole('button', { name: 'Collapse' }).getAttribute('aria-expanded')).toBe('true')
  })
})
