import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ContextBreakdown, UsageStats } from '@/types/hermes'

import { ContextUsagePanel } from './context-usage-panel'

const initialUsage: UsageStats = {
  calls: 1,
  context_max: 272_000,
  context_percent: 47,
  context_used: 128_200,
  input: 0,
  output: 0,
  total: 0
}

const breakdown: ContextBreakdown = {
  categories: [{ color: 'teal', id: 'conversation', label: 'Conversation', tokens: 241_400 }],
  context_max: 272_000,
  context_percent: 89,
  context_used: 241_400,
  context_used_is_estimate: false,
  estimated_total: 286_600,
  model: 'test-model'
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('ContextUsagePanel', () => {
  it('publishes once without refetching when publication recreates the callback', async () => {
    const requestGateway = vi.fn().mockResolvedValue(breakdown)
    const published = vi.fn()
    const renderedUsage: UsageStats[] = []

    function Harness() {
      const [currentUsage, setCurrentUsage] = useState(initialUsage)
      renderedUsage.push(currentUsage)

      return (
        <ContextUsagePanel
          currentUsage={currentUsage}
          onUsageSnapshot={snapshot => {
            published(snapshot)
            setCurrentUsage(current => ({ ...current, ...snapshot }))
          }}
          requestGateway={requestGateway}
          sessionId="runtime-1"
        />
      )
    }

    render(<Harness />)

    await waitFor(() => {
      expect(published).toHaveBeenCalledWith({
        context_max: 272_000,
        context_percent: 89,
        context_used: 241_400
      })
      expect(renderedUsage.at(-1)?.context_used).toBe(241_400)
    })
    await act(async () => {})

    expect(requestGateway).toHaveBeenCalledTimes(1)
    expect(requestGateway).toHaveBeenCalledWith('session.context_breakdown', { session_id: 'runtime-1' })
  })

  it('refetches when the session or gateway requester changes', async () => {
    const firstGateway = vi.fn().mockResolvedValue(breakdown)
    const secondGateway = vi.fn().mockResolvedValue(breakdown)

    const { rerender } = render(
      <ContextUsagePanel currentUsage={initialUsage} requestGateway={firstGateway} sessionId="runtime-1" />
    )

    await waitFor(() => expect(firstGateway).toHaveBeenCalledTimes(1))

    rerender(<ContextUsagePanel currentUsage={initialUsage} requestGateway={firstGateway} sessionId="runtime-2" />)

    await waitFor(() => {
      expect(firstGateway).toHaveBeenCalledTimes(2)
      expect(firstGateway).toHaveBeenLastCalledWith('session.context_breakdown', { session_id: 'runtime-2' })
    })

    rerender(<ContextUsagePanel currentUsage={initialUsage} requestGateway={secondGateway} sessionId="runtime-2" />)

    await waitFor(() => expect(secondGateway).toHaveBeenCalledTimes(1))
  })

  it('distinguishes the measured total from the estimated category breakdown', async () => {
    const requestGateway = vi.fn().mockResolvedValue(breakdown)

    render(<ContextUsagePanel currentUsage={initialUsage} requestGateway={requestGateway} sessionId="runtime-1" />)

    await waitFor(() => expect(requestGateway).toHaveBeenCalledTimes(1))

    expect(screen.getByText('Estimated breakdown')).toBeTruthy()
    expect(screen.getByText(/Category values are approximate/)).toBeTruthy()
    expect(document.querySelector('[data-slot="context-usage-total"]')?.textContent).not.toContain('~')
  })

  it('marks the total as approximate when no provider measurement exists', async () => {
    const requestGateway = vi.fn().mockResolvedValue({
      ...breakdown,
      context_used_is_estimate: true
    })

    render(<ContextUsagePanel currentUsage={initialUsage} requestGateway={requestGateway} sessionId="runtime-1" />)

    await waitFor(() => expect(requestGateway).toHaveBeenCalledTimes(1))

    expect(document.querySelector('[data-slot="context-usage-total"]')?.textContent).toContain('~')
  })
})
