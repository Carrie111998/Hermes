// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  getActionStatus: vi.fn(),
  restartGateway: vi.fn()
}))

vi.mock('@/hermes', () => api)

import { GatewayRestartConfirmation } from '@/components/gateway-restart-confirmation'
import { I18nProvider } from '@/i18n'
import { runGatewayRestart } from '@/store/system-actions'

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('GatewayRestartConfirmation', () => {
  it('executes a restart only after exact typed confirmation', async () => {
    vi.useFakeTimers()
    api.restartGateway.mockResolvedValue({ name: 'gateway-restart', ok: true })
    api.getActionStatus.mockResolvedValue({
      exit_code: 0,
      name: 'gateway-restart',
      running: false
    })

    render(
      <I18nProvider configClient={null} initialLocale="en">
        <GatewayRestartConfirmation />
      </I18nProvider>
    )

    let requested!: Promise<boolean>
    await act(async () => {
      requested = runGatewayRestart()
    })

    const confirm = screen.getByRole('button', { name: 'Confirm' }) as HTMLButtonElement
    expect(confirm.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText(/Type RESTART to confirm/i), {
      target: { value: 'RESTART' }
    })
    fireEvent.click(confirm)
    await act(async () => vi.advanceTimersByTimeAsync(1200))

    await expect(requested).resolves.toBe(true)
    expect(api.restartGateway).toHaveBeenCalledWith()
  })

  it('resolves the confirmation as false when the restart fails', async () => {
    vi.useFakeTimers()
    api.restartGateway.mockRejectedValue(new Error('restart failed'))

    render(
      <I18nProvider configClient={null} initialLocale="en">
        <GatewayRestartConfirmation />
      </I18nProvider>
    )

    let requested!: Promise<boolean>
    await act(async () => {
      requested = runGatewayRestart()
    })

    fireEvent.change(screen.getByLabelText(/Type RESTART to confirm/i), {
      target: { value: 'RESTART' }
    })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await act(async () => vi.advanceTimersByTimeAsync(1200))

    await expect(requested).resolves.toBe(false)
  })
})
