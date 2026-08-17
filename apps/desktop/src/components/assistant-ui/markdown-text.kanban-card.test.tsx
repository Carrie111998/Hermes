import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import {
  KANBAN_CARD_TOKEN_RE,
  kanbanCardMarkdownHref,
  kanbanCardRefFromMarkdownHref,
  linkifyKanbanCardRefs
} from '@/lib/kanban-card-refs'
import { preprocessMarkdown } from '@/lib/markdown-preprocess'

import { MarkdownTextContent } from './markdown-text'

afterEach(cleanup)

describe('kanban card refs in assistant markdown', () => {
  it('renders a t_ token as an inline ref with its card id', async () => {
    render(<MarkdownTextContent isRunning={false} text="See t_deadbeef in the renderer." />)

    const ref = await screen.findByText('t_deadbeef')

    expect(ref.tagName).toBe('A')
    expect(ref.className).toContain('ref')
    expect(ref.getAttribute('data-kanban-card')).toBe('t_deadbeef')
  })

  it('renders @card tokens while carrying the canonical t_ id', async () => {
    render(<MarkdownTextContent isRunning={false} text="See @card:deadbeef in the renderer." />)

    const ref = await screen.findByText('@card:deadbeef')

    expect(ref.tagName).toBe('A')
    expect(ref.getAttribute('data-kanban-card')).toBe('t_deadbeef')
  })

  it('leaves inline code and fenced code refs untouched', async () => {
    render(<MarkdownTextContent isRunning={false} text={'Use `t_deadbeef` here.\n\n```text\n@card:deadbeef\n```'} />)

    await screen.findByText('t_deadbeef')

    expect(document.querySelectorAll('[data-kanban-card]').length).toBe(0)
    expect(document.body.textContent).toContain('@card:deadbeef')
  })

  it('keeps URLs on the existing PrettyLink seam', async () => {
    render(<MarkdownTextContent isRunning={false} text="See https://example.com/docs and t_deadbeef." />)

    const url = await screen.findByTitle('https://example.com/docs')
    const ref = await screen.findByText('t_deadbeef')

    expect(url.tagName).toBe('A')
    expect(url.getAttribute('data-kanban-card')).toBeNull()
    expect(ref.getAttribute('data-kanban-card')).toBe('t_deadbeef')
  })
})

describe('kanban card ref preprocessing', () => {
  it('uses the plugin token rule and preserves negative-class boundaries', () => {
    expect(KANBAN_CARD_TOKEN_RE).toEqual(/(?<![A-Za-z0-9_])@card:[0-9a-f]{4,}|(?<![A-Za-z0-9_])t_[0-9a-f]{4,}/g)
    expect(linkifyKanbanCardRefs('xt_deadbeef xa@card:deadbeef t_deadbeef')).toBe(
      'xt_deadbeef xa@card:deadbeef [t_deadbeef](#kanban/t_deadbeef)'
    )
  })

  it('does not let an unrelated later link suppress a bracket-adjacent card ref', () => {
    expect(linkifyKanbanCardRefs('[t_deadbeef] and [docs](https://example.com)')).toBe(
      '[t_deadbeef](#kanban/t_deadbeef) and [docs](https://example.com)'
    )
  })

  it('does not nest a card ref inside a standalone bracket pair', () => {
    expect(linkifyKanbanCardRefs('[t_deadbeef]')).toBe('[t_deadbeef](#kanban/t_deadbeef)')
  })

  it('round-trips a canonical card id through the markdown href', () => {
    const href = kanbanCardMarkdownHref('t_deadbeef')

    expect(href).toBe('#kanban/t_deadbeef')
    expect(kanbanCardRefFromMarkdownHref(href)).toBe('t_deadbeef')
  })

  it('does not split a card-looking URL path into a card link', () => {
    const output = preprocessMarkdown('See https://example.com/t_deadbeef for details.')

    expect(output).toContain('<https://example.com/t_deadbeef>')
    expect(output).not.toContain('#kanban/')
  })
})
