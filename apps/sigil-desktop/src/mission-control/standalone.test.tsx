import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import { INITIAL_SIGIL_SNAPSHOT, MockSigilOperatorAdapter, SIGIL_FIRST_LAUNCH_LIMIT } from './mock-adapter'

import { SigilOperatorView } from './index'

describe('standalone Sigil Mission Control', () => {
  it('launches directly with Sigil-only product navigation and branding', async () => {
    render(<SigilOperatorView adapter={new MockSigilOperatorAdapter()} />)

    expect(await screen.findByTestId('sigil-operator')).toBeTruthy()
    expect(screen.getByText('Mission control')).toBeTruthy()

    for (const label of ['Overview', 'Proposals', 'Launch', 'Executions', 'Reconciliation', 'Audit', 'Settings']) {
      expect(screen.getByRole('button', { name: label })).toBeTruthy()
    }

    for (const hermesArea of ['Chat', 'Projects', 'Messaging', 'Artifacts']) {
      expect(screen.queryByText(hermesArea)).toBeNull()
    }
  })

  it('preserves paper safety, masked identity, the fixed cap, and reconciliation evidence', async () => {
    render(<SigilOperatorView adapter={new MockSigilOperatorAdapter('disconnected')} />)

    expect(await screen.findByText('•••• 23F4')).toBeTruthy()
    expect(screen.getAllByText(SIGIL_FIRST_LAUNCH_LIMIT).length).toBeGreaterThan(0)
    expect(screen.getAllByText('DISCONNECTED').length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: 'Reconciliation' }))
    expect(screen.getByText('Required')).toBeTruthy()
    expect(screen.queryByText(/submit order/i)).toBeNull()
  })

  it('confirmation-gates simulated approval and never exposes submission capability', async () => {
    const adapter = new MockSigilOperatorAdapter()
    render(<SigilOperatorView adapter={adapter} />)
    await screen.findByTestId('sigil-operator')

    fireEvent.click(screen.getByRole('button', { name: 'Proposals' }))
    fireEvent.click(screen.getByRole('button', { name: 'Enable simulated operator actions' }))
    fireEvent.click(screen.getAllByRole('button', { name: 'Approve' })[0]!)
    expect(screen.getByText('Confirm simulated approval')).toBeTruthy()
    expect((await adapter.readSnapshot()).proposals[0]?.status).toBe('pending')

    fireEvent.click(screen.getByRole('button', { name: 'Confirm approval' }))
    await waitFor(async () => expect((await adapter.readSnapshot()).proposals[0]?.status).toBe('approved'))
    expect((await adapter.readSnapshot()).auditEvents[0]?.details.broker_submission_attempted).toBe(false)
  })

  it('renders disconnected Hermes intelligence and explains the selected proposal locally', async () => {
    render(<SigilOperatorView adapter={new MockSigilOperatorAdapter()} />)
    await screen.findByTestId('sigil-operator')

    fireEvent.click(screen.getByRole('button', { name: 'Settings' }))

    expect(screen.getByText('Hermes Intelligence')).toBeTruthy()
    expect(screen.getByText('local-disconnected')).toBeTruthy()
    expect(screen.getByText('Analysis only')).toBeTruthy()
    expect(screen.getByText('Never')).toBeTruthy()
    expect(screen.getByText('Unavailable')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Explain selected proposal' }))

    const analysis = await screen.findByTestId('hermes-analysis')

    expect(analysis.textContent).toContain('Proposal PRP-20260725-0042 is governed and simulated.')
    expect(analysis.textContent).toContain('it cannot be submitted to a broker')
    expect(analysis.textContent).toContain('Execution authorized: no')
    expect(analysis.textContent).toContain('Broker submission available: no')
  })

  it('contains no broker submission method in the adapter contract', () => {
    expect('submitOrder' in new MockSigilOperatorAdapter()).toBe(false)
    expect(INITIAL_SIGIL_SNAPSHOT.environment).toBe('paper')
    expect(INITIAL_SIGIL_SNAPSHOT.simulation).toBe(true)
  })
})
