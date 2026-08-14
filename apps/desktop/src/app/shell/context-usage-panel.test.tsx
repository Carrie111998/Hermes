import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import type { ContextBreakdown, UsageStats } from '@/types/hermes'

import { ContextUsagePanel } from './context-usage-panel'

// Radix Select relies on browser APIs that jsdom does not implement.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

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
  compression_threshold_percent: 92,
  compression_threshold_tokens: 250_000,
  context_max: 272_000,
  context_percent: 89,
  context_used: 241_400,
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
    const compressNow = vi.fn()
    const published = vi.fn()
    const renderedUsage: UsageStats[] = []

    function Harness() {
      const [currentUsage, setCurrentUsage] = useState(initialUsage)
      renderedUsage.push(currentUsage)

      return (
        <ContextUsagePanel
          currentUsage={currentUsage}
          onCompressNow={compressNow}
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
        compression_threshold_percent: 92,
        compression_threshold_tokens: 250_000,
        context_max: 272_000,
        context_percent: 89,
        context_used: 241_400
      })
      expect(renderedUsage.at(-1)?.context_used).toBe(241_400)
      expect(screen.getByText('Automatic compression near 92% (250k tokens)')).not.toBeNull()
      expect(screen.getByText('8.6k tokens remaining')).not.toBeNull()
      expect(screen.getByRole('button', { name: 'Compress now' })).not.toBeNull()
      expect(screen.getByRole('combobox', { name: 'Keep recent turns' })).not.toBeNull()
    })

    fireEvent.click(screen.getByRole('combobox', { name: 'Keep recent turns' }))
    fireEvent.click(await screen.findByRole('option', { name: '4 turns' }))
    fireEvent.click(screen.getByRole('button', { name: 'Compress now' }))
    expect(compressNow).toHaveBeenCalledWith(4)
    await act(async () => {})

    expect(requestGateway).toHaveBeenCalledTimes(1)
    expect(requestGateway).toHaveBeenCalledWith('session.context_breakdown', { session_id: 'runtime-1' })
  })

  it('can retain the existing summarize-all behavior', async () => {
    const compressNow = vi.fn()

    render(
      <ContextUsagePanel
        currentUsage={initialUsage}
        onCompressNow={compressNow}
        requestGateway={vi.fn().mockResolvedValue(breakdown)}
        sessionId="runtime-1"
      />
    )

    const keepRecent = await screen.findByRole('combobox', { name: 'Keep recent turns' })
    fireEvent.click(keepRecent)
    fireEvent.click(await screen.findByRole('option', { name: 'Summarize all' }))
    fireEvent.click(screen.getByRole('button', { name: 'Compress now' }))

    expect(compressNow).toHaveBeenCalledWith(null)
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

  it('disables manual compression while the session is unavailable', async () => {
    const compressNow = vi.fn()

    render(
      <ContextUsagePanel
        compressNowDisabled
        currentUsage={initialUsage}
        onCompressNow={compressNow}
        requestGateway={vi.fn().mockResolvedValue(breakdown)}
        sessionId="runtime-1"
      />
    )

    const button = await screen.findByRole('button', { name: 'Compress now' })
    const keepRecent = screen.getByRole('combobox', { name: 'Keep recent turns' })
    expect((button as HTMLButtonElement).disabled).toBe(true)
    expect((keepRecent as HTMLButtonElement).disabled).toBe(true)

    fireEvent.click(button)
    expect(compressNow).not.toHaveBeenCalled()
  })

  it('does not offer manual compression before the warning range', async () => {
    const normalBreakdown: ContextBreakdown = {
      ...breakdown,
      context_percent: 37,
      context_used: 100_000
    }

    render(
      <ContextUsagePanel
        currentUsage={initialUsage}
        onCompressNow={vi.fn()}
        requestGateway={vi.fn().mockResolvedValue(normalBreakdown)}
        sessionId="runtime-1"
      />
    )

    await waitFor(() => {
      expect(screen.getByText('150k tokens remaining')).not.toBeNull()
    })
    expect(screen.queryByRole('button', { name: 'Compress now' })).toBeNull()
  })
})
