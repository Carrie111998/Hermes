import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { stubMenuDomApis, stubResizeObserver } from '@/test/jsdom'
import type { ContextBreakdown } from '@/types/hermes'

import { ContextStatusRow, contextUsageFromBreakdown } from './context-row'

stubResizeObserver()
stubMenuDomApis()

const breakdown: ContextBreakdown = {
  categories: [
    { color: 'var(--context-usage-system)', id: 'system_prompt', label: 'System prompt', tokens: 4000 },
    { color: 'var(--context-usage-conversation)', id: 'conversation', label: 'Conversation', tokens: 8000 }
  ],
  context_max: 200_000,
  context_percent: 6,
  context_used: 12_000,
  estimated_total: 12_000,
  model: 'claude-opus-5'
}

describe('contextUsageFromBreakdown', () => {
  it('carries the measured occupancy the backend reported', () => {
    expect(contextUsageFromBreakdown(breakdown)).toMatchObject({
      context_max: 200_000,
      context_percent: 6,
      context_used: 12_000
    })
  })

  it('zeroes out for a session with no breakdown yet, so the gauge stays hidden', () => {
    const usage = contextUsageFromBreakdown(null)

    expect(usage.context_max).toBe(0)
    expect(usage.total).toBe(0)
  })
})

describe('ContextStatusRow', () => {
  afterEach(cleanup)

  it('summarises used/max on the row without opening anything', () => {
    const view = render(
      <ContextStatusRow breakdown={breakdown} loading={false} usage={contextUsageFromBreakdown(breakdown)} />
    )

    const summary = view.container.querySelector('[data-slot="context-status-summary"]')

    expect(summary?.textContent).toContain('12k/200k')
    expect(summary?.textContent).toContain('6%')
    expect(screen.queryByText('Context Usage')).toBeNull()
  })

  it('opens the shared breakdown panel when the row is activated', async () => {
    render(<ContextStatusRow breakdown={breakdown} loading={false} usage={contextUsageFromBreakdown(breakdown)} />)

    fireEvent.click(screen.getByRole('button'))

    const panel = await screen.findByText('Context Usage')

    expect(panel).not.toBeNull()
    // Categories come from the breakdown, translated by the panel — proof the
    // row is feeding the real payload through rather than a summary of it.
    expect(screen.getByText('Conversation')).not.toBeNull()
    expect(screen.getByText('System prompt')).not.toBeNull()
  })
})
