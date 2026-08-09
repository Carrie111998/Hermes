import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { referenceRe, WIRE_REFERENCE_KINDS } from '@/components/assistant-ui/reference-kinds'

import { UserMessageText } from './user-message-text'

afterEach(cleanup)

/**
 * A sent reference must render as the chip the composer showed. These cover the
 * seam where that used to break: the value's quoting is directive syntax, and a
 * surface that reads it as markdown splits one reference into two wrong things.
 */
describe('a sent reference renders as the chip the composer showed', () => {
  it('chips a backtick-quoted @url: instead of splitting it into code', () => {
    render(
      <UserMessageText text="@url:`https://github.com/NousResearch/hermes-agent/pull/74790` urls lose formatting" />
    )

    expect(screen.queryByTitle('https://github.com/NousResearch/hermes-agent/pull/74790')).not.toBeNull()
    // The whole reference is one node — no bare `@url:` text left behind.
    expect(document.body.textContent).not.toContain('@url:')
  })

  it('chips a backtick-quoted @file: path with spaces', () => {
    render(<UserMessageText text="see @file:`apps/desktop/my notes.md` please" />)

    expect(screen.queryByTitle('apps/desktop/my notes.md')).not.toBeNull()
    expect(document.body.textContent).not.toContain('@file:')
  })

  it('chips every kind that travels in message text', () => {
    // The guard against WIRE_REFERENCE_KINDS and the pattern's own alternation
    // drifting apart: add a kind to one and this fails until both agree.
    for (const kind of WIRE_REFERENCE_KINDS) {
      expect(`@${kind}:\`some value\``.match(referenceRe()), kind).toHaveLength(1)
    }
  })

  it('still renders a genuine code span as code', () => {
    render(<UserMessageText text="run `npm test` first" />)

    const code = document.querySelector('[data-slot="aui_user-inline-code"]')

    expect(code?.textContent).toBe('npm test')
  })

  it('renders code and a reference side by side', () => {
    render(<UserMessageText text="run `npm test` on @file:`apps/desktop/a b.ts` now" />)

    expect(document.querySelector('[data-slot="aui_user-inline-code"]')?.textContent).toBe('npm test')
    expect(screen.queryByTitle('apps/desktop/a b.ts')).not.toBeNull()
  })

  it('leaves a fenced block alone', () => {
    render(<UserMessageText text={'before\n```ts\nconst x = 1\n```\nafter'} />)

    expect(document.querySelector('[data-slot="aui_user-fence"]')?.textContent).toBe('const x = 1\n')
  })

  it('renders an inline t_ token as a glyph-free kanban ref link', () => {
    render(<UserMessageText text="See t_deadbeef in the renderer." />)

    const ref = screen.getByText('t_deadbeef')

    expect(ref.tagName).toBe('A')
    expect(ref.className).toContain('ref')
    expect(ref.getAttribute('data-kanban-card')).toBe('t_deadbeef')
    expect(globalThis.document.querySelector('[data-ref]')).toBeNull()
  })

  it('leaves an inline-code t_ token untouched', () => {
    render(<UserMessageText text="Use `t_deadbeef` here." />)

    expect(globalThis.document.querySelector('[data-slot="aui_user-inline-code"]')?.textContent).toBe('t_deadbeef')
    expect(globalThis.document.querySelector('[data-kanban-card]')).toBeNull()
  })

  it('linkifies a bracket-adjacent token without nesting markdown brackets', () => {
    render(<UserMessageText text="[t_deadbeef] and [docs](https://example.com)" />)

    const ref = screen.getByText('t_deadbeef')

    expect(ref.tagName).toBe('A')
    expect(ref.getAttribute('data-kanban-card')).toBe('t_deadbeef')
    expect(ref.textContent).toBe('t_deadbeef')
    expect(globalThis.document.querySelectorAll('[data-kanban-card]')).toHaveLength(1)
  })

  it('renders @card tokens with the canonical t_ card id', () => {
    render(<UserMessageText text="See @card:deadbeef in the renderer." />)

    const ref = screen.getByText('@card:deadbeef')

    expect(ref.getAttribute('data-kanban-card')).toBe('t_deadbeef')
    expect(ref.getAttribute('href')).toBe('#kanban/t_deadbeef')
  })

  it('does not linkify a card-looking token inside a directive chip', () => {
    render(<UserMessageText text="See @file:t_deadbeef in the renderer." />)

    expect(screen.queryByTitle('t_deadbeef')).not.toBeNull()
    expect(globalThis.document.querySelector('[data-kanban-card]')).toBeNull()
  })

  it('preserves a mid-prose slash skill chip', () => {
    render(<UserMessageText text="Please run /clean on this." />)

    expect(globalThis.document.querySelector('[data-slot="aui_slash-chip"]')?.getAttribute('title')).toBe('/clean')
  })

  it('does not split a slash-shaped path inside a quoted directive value', () => {
    render(<UserMessageText text="Move @file:`/tmp/a /clean` now." />)

    expect(globalThis.document.querySelectorAll('[data-slot="aui_slash-chip"]')).toHaveLength(0)
    expect(screen.queryByTitle('/tmp/a /clean')).not.toBeNull()
  })

  it('leaves a card-looking URL untouched', () => {
    render(<UserMessageText text="See https://example.com/t_cafb2da4 for details." />)

    expect(globalThis.document.querySelector('[data-kanban-card]')).toBeNull()
    expect(globalThis.document.body.textContent).toContain('https://example.com/t_cafb2da4')
  })

  it('leaves a fenced card ref untouched', () => {
    render(<UserMessageText text={'before\n```text\nt_deadbeef\n```\nafter'} />)

    expect(globalThis.document.querySelector('[data-slot="aui_user-fence"]')?.textContent).toBe('t_deadbeef\n')
    expect(globalThis.document.querySelector('[data-kanban-card]')).toBeNull()
  })
})
