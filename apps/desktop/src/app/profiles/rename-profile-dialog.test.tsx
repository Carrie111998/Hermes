import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { renameProfile } from '@/hermes'
import { retireAgentGateways } from '@/store/gateway'

import { RenameProfileDialog } from './rename-profile-dialog'

// Pins the rename half of the deleted-profile-resurrection class (#88638 fixed
// the delete half): a retained renderer socket for the OLD profile name must be
// retired BEFORE the rename PATCH tears down its backend, or the socket's
// reconnect loop respawns the old-name backend and recreates the directory the
// rename just moved.

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

vi.mock('@/hermes', () => ({
  renameProfile: vi.fn(async () => ({ name: 'renamed', ok: true, path: '/x' }))
}))

vi.mock('@/store/gateway', () => ({
  retireAgentGateways: vi.fn()
}))

it('retires the old-name local gateways before issuing the rename', async () => {
  const order: string[] = []

  vi.mocked(retireAgentGateways).mockImplementationOnce(() => {
    order.push('retire')
  })
  vi.mocked(renameProfile).mockImplementationOnce(async () => {
    order.push('rename')

    return { name: 'renamed', ok: true, path: '/x' }
  })

  render(<RenameProfileDialog currentName="selena" onClose={vi.fn()} open />)

  fireEvent.change(screen.getByLabelText(/new name/i), { target: { value: 'renamed' } })
  fireEvent.click(screen.getByRole('button', { name: /^rename$/i }))

  await waitFor(() => expect(renameProfile).toHaveBeenCalledWith('selena', 'renamed'))
  // Owner-scoped: null is "the legacy/primary-route pool", which is what an
  // un-scoped dialog (single gateway) means. A registry row passes its own id
  // so the same-named profile on another machine is left alone (#88638).
  expect(retireAgentGateways).toHaveBeenCalledWith(null, 'selena')
  expect(order).toEqual(['retire', 'rename'])
})

it('does not retire gateways when validation rejects the submit', async () => {
  render(<RenameProfileDialog currentName="selena" onClose={vi.fn()} open />)

  fireEvent.change(screen.getByLabelText(/new name/i), { target: { value: '' } })
  fireEvent.click(screen.getByRole('button', { name: /^rename$/i }))

  await waitFor(() => expect(screen.getByText('Name is required.')).toBeTruthy())
  expect(retireAgentGateways).not.toHaveBeenCalled()
  expect(renameProfile).not.toHaveBeenCalled()
})


it('retires the OWNING machine\'s gateways when the row belongs to another connection', async () => {
  render(<RenameProfileDialog connectionId="mechahome-hermes-dell" currentName="selena" onClose={vi.fn()} open />)

  fireEvent.change(screen.getByLabelText(/new name/i), { target: { value: 'renamed' } })
  fireEvent.click(screen.getByRole('button', { name: /^rename$/i }))

  await waitFor(() => expect(renameProfile).toHaveBeenCalledWith('selena', 'renamed', 'mechahome-hermes-dell'))

  // The local-only seam would have retired the LOCAL `selena`'s sockets while
  // renaming the Dell's — tearing down a machine the row does not own.
  expect(retireAgentGateways).toHaveBeenCalledWith('mechahome-hermes-dell', 'selena')
})
