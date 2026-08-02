import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { LinkedDevice } from '@/types/hermes'

const getLinkedDevices = vi.fn()
const revokeLinkedDevice = vi.fn()
const notify = vi.fn()
const notifyError = vi.fn()

vi.mock('@/hermes', () => ({
  getLinkedDevices: () => getLinkedDevices(),
  revokeLinkedDevice: (id: string) => revokeLinkedDevice(id)
}))

vi.mock('@/store/notifications', () => ({ notify, notifyError }))

const phone: LinkedDevice = {
  created_at: 1_786_000_000,
  id: 'dev_phone',
  label: 'iPhone',
  last_seen_at: 1_786_086_400
}

beforeEach(() => {
  getLinkedDevices.mockResolvedValue({ devices: [] })
  revokeLinkedDevice.mockResolvedValue({ ok: true })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('LinkedDevicesSettings', () => {
  it('shows loading, then the empty state', async () => {
    let resolve: ((value: { devices: LinkedDevice[] }) => void) | undefined
    getLinkedDevices.mockReturnValue(
      new Promise(done => {
        resolve = done
      })
    )
    const { LinkedDevicesSettings } = await import('./linked-devices-settings')

    render(<LinkedDevicesSettings />)
    expect(screen.getByText('Loading linked devices…')).toBeTruthy()

    resolve?.({ devices: [] })
    expect(await screen.findByText('No linked devices')).toBeTruthy()
  })

  it('retries after a load error', async () => {
    getLinkedDevices.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ devices: [] })
    const { LinkedDevicesSettings } = await import('./linked-devices-settings')

    render(<LinkedDevicesSettings />)
    expect(await screen.findByText('Could not load linked devices.')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('No linked devices')).toBeTruthy()
    expect(getLinkedDevices).toHaveBeenCalledTimes(2)
  })

  it('renders one safe row per device id', async () => {
    getLinkedDevices.mockResolvedValue({ devices: [phone, phone] })
    const { LinkedDevicesSettings } = await import('./linked-devices-settings')

    render(<LinkedDevicesSettings />)

    expect(await screen.findByText('iPhone')).toBeTruthy()
    expect(screen.getAllByText('iPhone')).toHaveLength(1)
    expect(screen.getByText(/Linked .* Last used/)).toBeTruthy()
  })

  it('requires confirmation and honours cancel', async () => {
    getLinkedDevices.mockResolvedValue({ devices: [phone] })
    const { LinkedDevicesSettings } = await import('./linked-devices-settings')

    render(<LinkedDevicesSettings />)
    fireEvent.click(await screen.findByRole('button', { name: 'Revoke' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(revokeLinkedDevice).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Revoke' })).toBeTruthy()
  })

  it('revokes a confirmed device and removes its row', async () => {
    getLinkedDevices.mockResolvedValue({ devices: [phone] })
    const { LinkedDevicesSettings } = await import('./linked-devices-settings')

    render(<LinkedDevicesSettings />)
    fireEvent.click(await screen.findByRole('button', { name: 'Revoke' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))

    await waitFor(() => expect(revokeLinkedDevice).toHaveBeenCalledWith(phone.id))
    expect(await screen.findByText('No linked devices')).toBeTruthy()
    expect(notify).toHaveBeenCalledWith(expect.objectContaining({ kind: 'success', message: 'Linked device revoked.' }))
  })

  it('keeps the device and reports a failed revoke', async () => {
    const error = new Error('failed')
    getLinkedDevices.mockResolvedValue({ devices: [phone] })
    revokeLinkedDevice.mockRejectedValue(error)
    const { LinkedDevicesSettings } = await import('./linked-devices-settings')

    render(<LinkedDevicesSettings />)
    fireEvent.click(await screen.findByRole('button', { name: 'Revoke' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))

    await waitFor(() => expect(notifyError).toHaveBeenCalledWith(error, 'Could not revoke linked device.'))
    expect(screen.getByText('iPhone')).toBeTruthy()
  })
})
