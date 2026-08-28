import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { $compactingSessions } from '@/store/compaction'
import { _resetContextBreakdownForTests } from '@/store/context-breakdown'
import { stubMenuDomApis, stubResizeObserver } from '@/test/jsdom'
import type { ContextBreakdown } from '@/types/hermes'

import { ContextStatusRow, contextUsageFromBreakdown } from './context-row'

stubResizeObserver()
stubMenuDomApis()

const breakdown: ContextBreakdown = {
  categories: [
    { color: 'var(--context-usage-files)', count: 9, id: 'files', label: 'Files', tokens: 56_200 },
    { color: 'var(--context-usage-conversation)', id: 'conversation', label: 'Conversation', tokens: 78_500 }
  ],
  context_max: 900_000,
  context_percent: 21,
  context_used: 192_200,
  estimated_total: 192_200,
  model: 'claude-opus-5'
}

function gatewayFor(payload: ContextBreakdown | null) {
  return { request: vi.fn().mockResolvedValue(payload) } as never
}

afterEach(() => {
  cleanup()
  _resetContextBreakdownForTests()
  $compactingSessions.set({})
  vi.restoreAllMocks()
})

describe('contextUsageFromBreakdown', () => {
  it('carries the measured occupancy the backend reported', () => {
    expect(contextUsageFromBreakdown(breakdown)).toMatchObject({
      context_max: 900_000,
      context_percent: 21,
      context_used: 192_200
    })
  })

  it('zeroes out for a session with no breakdown yet, so the gauge stays hidden', () => {
    const usage = contextUsageFromBreakdown(null)

    expect(usage.context_max).toBe(0)
    expect(usage.total).toBe(0)
  })
})

describe('ContextStatusRow', () => {
  it('summarises used/max and percent once the session reports a window', async () => {
    const view = render(<ContextStatusRow busy={false} gateway={gatewayFor(breakdown)} sessionId="s1" />)

    await waitFor(() => expect(view.container.querySelector('[data-slot="context-status-pill"]')).not.toBeNull())
    expect(view.container.querySelector('[data-slot="context-status-summary"]')?.textContent).toBe('192.2k/900k')
    expect(screen.getByText('21%')).not.toBeNull()
  })

  it('renders nothing until the session has a context window', async () => {
    const view = render(<ContextStatusRow busy={false} gateway={gatewayFor(null)} sessionId="s1" />)

    await waitFor(() => expect(view.container.querySelector('[data-slot="context-status-pill"]')).toBeNull())
  })

  it('opens the shared breakdown panel, file count and all', async () => {
    render(<ContextStatusRow busy={false} gateway={gatewayFor(breakdown)} sessionId="s1" />)

    fireEvent.click(await screen.findByText('192.2k/900k'))

    expect(await screen.findByText('Context Usage')).not.toBeNull()
    expect(screen.getByText('Files')).not.toBeNull()
    expect(screen.getByText('9 files')).not.toBeNull()
  })

  it('submits the compaction command from the Compact pill', async () => {
    const onCompact = vi.fn()
    render(
      <ContextStatusRow busy={false} gateway={gatewayFor(breakdown)} onCompact={onCompact} sessionId="s1" />
    )

    fireEvent.click(await screen.findByText('Compact'))

    expect(onCompact).toHaveBeenCalledTimes(1)
  })

  it('omits the Compact pill when the surface offers no compaction path', async () => {
    render(<ContextStatusRow busy={false} gateway={gatewayFor(breakdown)} sessionId="s1" />)

    await screen.findByText('192.2k/900k')
    expect(screen.queryByText('Compact')).toBeNull()
  })

  it('disables Compact mid-turn — the transcript is still moving', async () => {
    const onCompact = vi.fn()

    const view = render(
      <ContextStatusRow busy gateway={gatewayFor(breakdown)} onCompact={onCompact} sessionId="s1" />
    )

    // Busy also suppresses the estimate, so seed the store through a settled
    // render first, then re-render busy.
    view.rerender(<ContextStatusRow busy={false} gateway={gatewayFor(breakdown)} onCompact={onCompact} sessionId="s1" />)
    await screen.findByText('192.2k/900k')
    view.rerender(<ContextStatusRow busy gateway={gatewayFor(breakdown)} onCompact={onCompact} sessionId="s1" />)

    const pill = view.container.querySelector<HTMLButtonElement>('[data-slot="context-compact-pill"]')

    expect(pill?.disabled).toBe(true)
    fireEvent.click(pill!)
    expect(onCompact).not.toHaveBeenCalled()
  })

  it('shows compaction in progress instead of offering it again', async () => {
    $compactingSessions.set({ s1: true })
    render(<ContextStatusRow busy={false} gateway={gatewayFor(breakdown)} onCompact={vi.fn()} sessionId="s1" />)

    expect(await screen.findByText('Compacting…')).not.toBeNull()
    expect(screen.queryByText('Compact')).toBeNull()
  })
})

describe('ContextStatusRow compaction recovery', () => {
  it('shows progress while the submit is in flight and releases when it settles', async () => {
    let settle: () => void = () => undefined
    const onCompact = vi.fn(() => new Promise<void>(resolve => (settle = resolve)))

    render(<ContextStatusRow busy={false} gateway={gatewayFor(breakdown)} onCompact={onCompact} sessionId="s1" />)

    fireEvent.click(await screen.findByText('Compact'))
    expect(await screen.findByText('Compacting…')).not.toBeNull()

    settle()

    expect(await screen.findByText('Compact')).not.toBeNull()
  })

  it('recovers when the compaction request fails — a timed-out RPC must not wedge the pill', async () => {
    // The gateway's `status: compacting` event clears on the NEXT stream event.
    // If the socket drops, or the RPC times out while the server keeps going,
    // that event may never arrive; the pill has to let go on its own.
    const onCompact = vi.fn().mockRejectedValue(new Error('request timed out after 120s'))

    render(<ContextStatusRow busy={false} gateway={gatewayFor(breakdown)} onCompact={onCompact} sessionId="s1" />)

    fireEvent.click(await screen.findByText('Compact'))

    const pill = await screen.findByText('Compact')

    expect(pill).not.toBeNull()
    expect(screen.queryByText('Compacting…')).toBeNull()
  })
})
