import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { clearAllSessionStates, publishSessionState } from '@/store/session-states'

import { GroupStatusDot } from './session-status-dot'

afterEach(() => {
  cleanup()
  clearAllSessionStates()
})

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      sidebar: {
        row: {
          backgroundRunning: 'Running in background',
          draftSession: 'Draft',
          finishedUnread: 'Finished',
          needsInput: 'Needs input',
          sessionRunning: 'Running',
          waitingForAnswer: 'Waiting for answer'
        }
      }
    }
  })
}))

describe('GroupStatusDot', () => {
  it('renders the idle fallback when every member is idle', () => {
    render(<GroupStatusDot idle={<span data-testid="lane-glyph" />} sessionIds={['a', 'b']} />)

    expect(screen.getByTestId('lane-glyph')).toBeTruthy()
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('renders nothing at idle when no fallback is given (project row slot)', () => {
    const { container } = render(<GroupStatusDot sessionIds={['a']} />)

    expect(container.innerHTML).toBe('')
  })

  it('replaces the fallback with the loudest member state', () => {
    publishSessionState('rt-work', { ...createClientSessionState('stored-work'), busy: true })
    publishSessionState('rt-ask', { ...createClientSessionState('stored-ask'), needsInput: true })

    render(<GroupStatusDot idle={<span data-testid="lane-glyph" />} sessionIds={['stored-work', 'stored-ask']} />)

    // needs-input outranks working. The glyph yields to the dot.
    expect(screen.getByRole('status', { name: 'Needs input' })).toBeTruthy()
    expect(screen.queryByTestId('lane-glyph')).toBeNull()
  })

  it('paints the member state through the same variant table as the session dot', () => {
    publishSessionState('rt-work', { ...createClientSessionState('stored-work'), busy: true })

    render(<GroupStatusDot sessionIds={['stored-work']} />)

    expect(screen.getByRole('status', { name: 'Running' })).toBeTruthy()
  })

  it('ignores ids outside the group', () => {
    publishSessionState('rt-other', { ...createClientSessionState('stored-other'), busy: true })

    render(<GroupStatusDot idle={<span data-testid="lane-glyph" />} sessionIds={['stored-quiet']} />)

    expect(screen.getByTestId('lane-glyph')).toBeTruthy()
  })
})
