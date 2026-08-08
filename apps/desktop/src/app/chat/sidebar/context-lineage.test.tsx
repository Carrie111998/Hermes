import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { CompressionLineage } from '@/hermes'

import { ContextLineage } from './context-lineage'

const mocks = vi.hoisted(() => ({
  getCompressionSegmentMessages: vi.fn()
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: {
        close: 'Close',
        retry: 'Retry'
      },
      sidebar: {
        lineage: {
          all: 'View all segments',
          branch: 'Branch from this segment',
          current: 'Current segment',
          error: 'Could not load this segment.',
          loading: 'Loading segment…',
          readOnly: 'Read-only historical segment',
          segment: (index: number) => `Segment ${index}`,
          segments: (count: number) => `${count} segments`,
          title: 'Conversation context'
        }
      }
    }
  })
}))

vi.mock('@/components/assistant-ui/thread', () => ({
  Thread: () => <div>read-only thread</div>
}))

vi.mock('@/hermes', async importOriginal => {
  const actual = await importOriginal<Record<string, unknown>>()

  return {
    ...actual,
    getCompressionSegmentMessages: (...args: unknown[]) => mocks.getCompressionSegmentMessages(...args)
  }
})

const lineage: CompressionLineage = {
  root_session_id: 'root',
  segments: [
    { id: 'root', index: 1, is_tip: false, message_count: 2, started_at: 10, title: 'Start' },
    { id: 'middle', index: 2, is_tip: false, message_count: 3, started_at: 20, title: 'Middle' },
    { id: 'tip', index: 3, is_tip: true, message_count: 4, started_at: 30, title: 'Current' }
  ],
  tip_session_id: 'tip'
}

describe('ContextLineage', () => {
  it('shows the current segment and two recent historical segments compactly', () => {
    render(<ContextLineage lineage={lineage} />)

    const compact = screen.getByTestId('lineage-compact')
    const trigger = screen.getByRole('button', { name: '3 segments' })

    expect(compact.textContent).toContain('Current segment')
    expect(compact.textContent).toContain('Segment 2')
    expect(compact.textContent).toContain('Segment 1')
    expect(compact.textContent).toContain('View all segments')
    expect(trigger.getAttribute('aria-haspopup')).toBe('dialog')
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    expect(mocks.getCompressionSegmentMessages).not.toHaveBeenCalled()
  })

  it('loads an exact historical segment without resuming the live session', async () => {
    mocks.getCompressionSegmentMessages.mockResolvedValue({
      messages: [{ content: 'old answer', role: 'assistant' }],
      session_id: 'middle'
    })
    const onBranch = vi.fn()

    render(<ContextLineage lineage={lineage} onBranch={onBranch} profile="work" />)

    fireEvent.click(screen.getByRole('button', { name: '3 segments' }))
    const segmentButtons = screen.getAllByRole('button', { name: 'Segment 2' })
    fireEvent.click(segmentButtons.at(-1)!)

    await waitFor(() => expect(mocks.getCompressionSegmentMessages).toHaveBeenCalledWith('tip', 'middle', 'work'))
    expect(await screen.findByTestId('lineage-transcript')).toBeTruthy()
    expect(screen.getByText('Read-only historical segment')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Branch from this segment' }))
    expect(onBranch).toHaveBeenCalledWith('middle', 'work', 'tip')
  })

  it('shows an error and retries instead of treating a failed segment as empty', async () => {
    mocks.getCompressionSegmentMessages
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({ messages: [], session_id: 'middle' })
    const onBranch = vi.fn()

    render(<ContextLineage lineage={lineage} onBranch={onBranch} profile="work" />)

    fireEvent.click(screen.getByRole('button', { name: '3 segments' }))
    fireEvent.click(screen.getAllByRole('button', { name: 'Segment 2' }).at(-1)!)

    expect((await screen.findByRole('alert')).textContent).toContain('Could not load this segment.')
    expect(screen.queryByRole('button', { name: 'Branch from this segment' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    await waitFor(() => expect(mocks.getCompressionSegmentMessages).toHaveBeenCalledTimes(2))
    expect(await screen.findByTestId('lineage-transcript')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Branch from this segment' })).toBeTruthy()
  })

  it('does not reuse a historical cache across profiles with the same tip id', async () => {
    mocks.getCompressionSegmentMessages
      .mockResolvedValueOnce({ messages: [{ content: 'work history', role: 'assistant' }], session_id: 'middle' })
      .mockResolvedValueOnce({ messages: [{ content: 'personal history', role: 'assistant' }], session_id: 'middle' })

    const view = render(<ContextLineage lineage={lineage} profile="work" />)
    fireEvent.click(screen.getByRole('button', { name: '3 segments' }))
    fireEvent.click(screen.getAllByRole('button', { name: 'Segment 2' }).at(-1)!)
    expect(await screen.findByTestId('lineage-transcript')).toBeTruthy()
    expect(mocks.getCompressionSegmentMessages).toHaveBeenCalledWith('tip', 'middle', 'work')

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    view.rerender(<ContextLineage lineage={lineage} profile="personal" />)
    fireEvent.click(screen.getByRole('button', { name: '3 segments' }))
    fireEvent.click(screen.getAllByRole('button', { name: 'Segment 2' }).at(-1)!)

    await waitFor(() =>
      expect(mocks.getCompressionSegmentMessages).toHaveBeenLastCalledWith('tip', 'middle', 'personal')
    )
    expect(mocks.getCompressionSegmentMessages).toHaveBeenCalledTimes(2)
  })
})
