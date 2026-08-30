import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { MoaProgressState } from '@/lib/moa-progress'

import { MoaOrchestration } from './moa-orchestration'

vi.mock('thinking-orbs', () => ({
  ThinkingOrb: ({ state }: { state: string }) => <span data-testid="thinking-orb">{state}</span>
}))

const liveState: MoaProgressState = {
  advisors: [
    { index: 1, label: 'model-a', status: 'running' },
    { index: 2, label: 'model-b', output: 'Use the indexed protocol.', status: 'complete' },
    { index: 3, label: 'model-c', status: 'failed' }
  ],
  aggregator: 'agg-model',
  concurrency: 3,
  fanout: 'user_turn',
  guidanceReused: false,
  phase: 'reference',
  startedAt: 10
}

describe('MoaOrchestration', () => {
  it('renders one compact parallel row and reveals advisor details on demand', () => {
    render(<MoaOrchestration state={liveState} />)

    const summary = screen.getByLabelText(/2\/3 advisors in parallel.*aggregator waiting/i)

    expect(screen.getByRole('status').textContent).toMatch(/2\/3 advisors in parallel.*aggregator waiting/i)
    expect(screen.getByTestId('thinking-orb').textContent).toBe('solving')
    const output = screen.getByText('Use the indexed protocol.')
    const advisorDetails = output.closest('details')

    expect(advisorDetails?.open).toBe(false)

    fireEvent.click(summary)
    fireEvent.click(screen.getByText('model-b').closest('summary')!)
    expect(advisorDetails?.open).toBe(true)
    expect(screen.getAllByLabelText('model-c: failed')).toHaveLength(2)
  })

  it('settles to a compact advisor-to-aggregator summary without an active orb', () => {
    render(<MoaOrchestration state={{ ...liveState, phase: 'settled', settledAt: 28.2 }} />)

    expect(screen.getByLabelText(/3 advisors.*agg-model.*18\.2s/i)).not.toBeNull()
    expect(screen.queryByTestId('thinking-orb')).toBeNull()
  })

  it('labels cached guidance reuse instead of claiming a fresh fan-out', () => {
    render(<MoaOrchestration state={{ ...liveState, guidanceReused: true }} />)

    expect(screen.getByLabelText(/guidance reused/i)).not.toBeNull()
    expect(screen.getByTestId('thinking-orb').textContent).toBe('breathing')
    expect(screen.queryByText('parallel')).toBeNull()
  })

  it.each(['aggregating', 'settled'] as const)('keeps cache reuse visible while %s', phase => {
    render(<MoaOrchestration state={{ ...liveState, guidanceReused: true, phase }} />)

    expect(screen.getByLabelText(/guidance reused/i)).not.toBeNull()
  })
})
