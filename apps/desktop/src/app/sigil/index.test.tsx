import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { MockSigilOperatorAdapter } from './mock-adapter'

import { SigilOperatorView } from './index'

describe('SigilOperatorView', () => {
  it('is paper, simulated, disconnected, masked, and has no live submission control', async () => {
    render(<SigilOperatorView adapter={new MockSigilOperatorAdapter('ready')} />)

    expect(await screen.findByText('Sigil Operator')).toBeTruthy()
    expect(screen.getByText('PAPER')).toBeTruthy()
    expect(screen.getByText('SIMULATED')).toBeTruthy()
    expect(screen.getByText('DISCONNECTED')).toBeTruthy()
    expect(screen.getByText('•••• 23F4')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /submit/i })).toBeNull()
    expect(screen.getByText(/No broker submission available/)).toBeTruthy()
  })

  it('keeps proposal approval locked until enabled and explicitly confirmed', async () => {
    const adapter = new MockSigilOperatorAdapter('ready')
    const apply = vi.spyOn(adapter, 'applySimulatedAction')
    render(<SigilOperatorView adapter={adapter} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Proposals' }))
    expect(screen.getAllByRole('button', { name: 'Approve' })[0].hasAttribute('disabled')).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'Enable simulated operator actions' }))
    fireEvent.click(screen.getAllByRole('button', { name: 'Approve' })[0])
    expect(screen.getByText('Confirm simulated approval')).toBeTruthy()
    expect(apply).not.toHaveBeenCalled()

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Confirm approval' }))
    })

    await waitFor(() =>
      expect(apply).toHaveBeenCalledWith({ type: 'approve-proposal', proposalId: 'PRP-20260725-0042' })
    )
  })

  it.each(['empty', 'disconnected', 'stale'] as const)('renders the %s snapshot state', async dataState => {
    render(<SigilOperatorView adapter={new MockSigilOperatorAdapter(dataState)} />)
    expect(await screen.findByTestId('sigil-operator')).toBeTruthy()
  })

  it('renders a recoverable error state', async () => {
    render(<SigilOperatorView adapter={new MockSigilOperatorAdapter('error')} />)
    expect(await screen.findByText('Sigil snapshot unavailable')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Retry local snapshot' })).toBeTruthy()
  })
})
