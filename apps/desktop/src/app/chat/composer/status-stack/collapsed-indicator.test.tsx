import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { $todosBySession } from '@/store/todos'

import { ComposerStatusStack } from './index'

describe('ComposerStatusStack collapsed todo indicator', () => {
  beforeAll(() => {
    vi.stubGlobal(
      'ResizeObserver',
      class {
        disconnect() {}
        observe() {}
      }
    )
  })

  afterEach(() => {
    cleanup()
    $todosBySession.set({})
  })

  it('shows only the active todo next to the collapsed todo label', () => {
    $todosBySession.set({
      'session-1': [
        { content: 'Finished setup', id: '1', status: 'completed' },
        { content: 'Wire the status stack', id: '2', status: 'in_progress' },
        { content: 'Run verification', id: '3', status: 'pending' }
      ]
    })

    render(
      <MemoryRouter>
        <ComposerStatusStack queue={null} sessionId="session-1" />
      </MemoryRouter>
    )

    const button = screen.getByRole('button', { name: /Tasks 1\/3/ })

    expect(screen.getByText('Finished setup')).toBeTruthy()
    expect(screen.getByText('Wire the status stack')).toBeTruthy()
    expect(screen.getByText('Run verification')).toBeTruthy()

    fireEvent.click(button)

    const label = screen.getByText('Tasks 1/3')
    const indicator = screen.getByRole('status')
    const activeTodo = screen.getByText('Wire the status stack')

    expect(screen.queryByText('Finished setup')).toBeNull()
    expect(screen.queryByText('Run verification')).toBeNull()
    expect(button.contains(indicator)).toBe(true)
    expect(button.contains(activeTodo)).toBe(true)
    expect(label.compareDocumentPosition(indicator) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(indicator.compareDocumentPosition(activeTodo) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('tracks the active todo while the section stays collapsed', () => {
    $todosBySession.set({
      'session-1': [
        { content: 'First active task', id: '1', status: 'in_progress' },
        { content: 'Next active task', id: '2', status: 'pending' }
      ]
    })

    render(
      <MemoryRouter>
        <ComposerStatusStack queue={null} sessionId="session-1" />
      </MemoryRouter>
    )

    const button = screen.getByRole('button', { name: /Tasks 0\/2/ })
    fireEvent.click(button)

    expect(button.contains(screen.getByText('First active task'))).toBe(true)
    expect(screen.queryByText('Next active task')).toBeNull()

    act(() => {
      $todosBySession.set({
        'session-1': [
          { content: 'First active task', id: '1', status: 'completed' },
          { content: 'Next active task', id: '2', status: 'in_progress' }
        ]
      })
    })

    expect(screen.queryByText('First active task')).toBeNull()
    expect(button.contains(screen.getByText('Next active task'))).toBe(true)
    expect(screen.getAllByRole('status')).toHaveLength(1)
  })

  it('does not show inactive todos or a collapsed indicator when no todo is running', () => {
    $todosBySession.set({
      'session-1': [
        { content: 'Finished setup', id: '1', status: 'completed' },
        { content: 'Run verification', id: '2', status: 'pending' }
      ]
    })

    render(
      <MemoryRouter>
        <ComposerStatusStack queue={null} sessionId="session-1" />
      </MemoryRouter>
    )

    fireEvent.click(screen.getByRole('button', { name: /Tasks 1\/2/ }))

    expect(screen.queryByText('Finished setup')).toBeNull()
    expect(screen.queryByText('Run verification')).toBeNull()
    expect(screen.queryByRole('status')).toBeNull()
  })
})
